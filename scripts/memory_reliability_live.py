"""60-turn live API replay with the real durable job dispatcher (isolated data only)."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from sqlalchemy import select
from app.agents.runtime import RuntimeInput
from app.agents.prompts import RESPONSE_SYSTEM_PROMPT
from app.core.config import Settings
from app.db import Base, build_engine, build_session_factory
from app.memory.index import MemoryIndex
from app.memory.repository import memory_dict
from app.memory.service import MemoryService
from app.memory.store import InMemoryRedisStore
from app.models import Conversation, ConversationSummary, MemoryBatch, MemoryEvent, Message, ToolJob, User, UserMemory, utc_now
from app.services.model import ModelMessage, build_model_provider
from app.services.tool_dispatcher import InlineToolDispatcher
from scripts.memory_live_eval import STUDENT_TURNS, ASSISTANT_TURNS, ASSISTANT_OVERRIDES, PROBES, RecordingProvider, write_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--run-id", default="reliability-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--max-calls", type=int, default=120)
    args = parser.parse_args()
    if not args.live or not 10 <= args.max_calls <= 160:
        parser.error("--live and a 10..160 call budget are required")
    if Path(args.run_id).name != args.run_id or args.run_id in (".", ".."):
        parser.error("run-id must be one directory name")
    output = ROOT / "tmp" / "memory-live" / args.run_id
    output.mkdir(parents=True, exist_ok=False)
    settings = Settings().model_copy(update={"app_env": "memory_live",
        "database_url": f"sqlite:///{(output / 'memory.db').as_posix()}",
        "rag_chroma_path": str(output / "chroma"), "memory_collection_name": "memory_reliability_v2",
        "memory_foreground_model_calls": False, "model_max_retries": 0,
        "memory_idle_seconds": 0, "memory_batch_retry_seconds": 0})
    if not settings.model_api_key or settings.model_provider == "mock":
        raise RuntimeError("a real configured model and API key are required")
    engine = build_engine(settings.database_url)
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    db = factory()
    provider = RecordingProvider(build_model_provider(settings), output, limit=args.max_calls)
    service = MemoryService(settings, InMemoryRedisStore(), provider, index=MemoryIndex(settings))
    dispatcher = InlineToolDispatcher(factory, settings)
    dispatcher.memory_service = service
    result = {"status": "running", "model": settings.model_name, "max_calls": args.max_calls,
              "snapshots": [], "probes": [], "errors": [], "foreground_model_calls": 0,
              "note": "Synthetic student turns and fixed assistant replies; real extraction, correction, retrieval and probe API. Durable queue uses InlineToolDispatcher."}

    def save():
        db.expire_all()
        result["api_calls"] = len(provider.calls)
        result["memories"] = [memory_dict(r) for r in db.scalars(select(UserMemory))]
        result["batches"] = [{"key": r.batch_key, "status": r.status, "attempts": r.attempts, "error": r.error}
                             for r in db.scalars(select(MemoryBatch))]
        result["events"] = [{"memory_id": e.memory_id, "relation": e.relation, "status": e.status,
                             "source": e.source_message_id, "quote": e.quote} for e in db.scalars(select(MemoryEvent))]
        result["jobs"] = [{"type": j.job_type, "status": j.status, "error": j.last_error}
                          for j in db.scalars(select(ToolJob))]
        result["usage"] = {key: sum(c.get("response", {}).get(key, 0) for c in provider.calls)
                           for key in ("input_tokens", "output_tokens")}
        write_json(output / "result.json", result)
        db.commit()

    def drain(retry=False):
        db.commit()
        if retry:
            for job in db.scalars(select(ToolJob).where(ToolJob.status == "PENDING")):
                job.run_after = utc_now() - timedelta(seconds=1)
            db.commit()
        dispatcher.dispatch()
        db.expire_all()
        save()

    def conversation(student):
        c = Conversation(student_id=student.id)
        db.add(c)
        db.commit()
        return c

    def message(c, text, role="student", status="completed"):
        m = Message(conversation_id=c.id, sender_type=role, content=text, redacted_content=text, status=status)
        db.add(m)
        db.commit()
        return m

    def probe(key, c, question):
        ctx = service.load(db, c, current_input=question)
        messages = [ModelMessage("system", RESPONSE_SYSTEM_PROMPT), *RuntimeInput(question, ctx).history_prompt_messages,
                    ModelMessage("user", question)]
        item = {"id": key, "question": question, "context": asdict(ctx)}
        try:
            response = provider.complete(messages, model=settings.model_response_name or settings.model_name,
                                         temperature=0, json_mode=False, purpose="response_generation")
            item["answer"] = response.content
        except Exception as exc:
            item["error"] = str(exc)
        result["probes"].append(item)
        print("PROBE", key, item.get("answer", item.get("error")), flush=True)
        save()

    try:
        owner = User(institutional_id="reliability_owner", name="test", password_hash="unused", role="student")
        db.add(owner)
        db.commit()
        c = conversation(owner)
        for turn, text in enumerate(STUDENT_TURNS, 1):
            current = message(c, text, status="generating")
            before = len(provider.calls)
            ctx = service.load(db, c, current_input=text, current_message=current)
            result["foreground_model_calls"] += len(provider.calls) - before
            current.status = "completed"
            db.commit()
            reply = message(c, ASSISTANT_OVERRIDES.get(turn, ASSISTANT_TURNS[(turn - 1) % 3]), role="assistant")
            service.enqueue_after_turn(db, c, current, reply)
            db.commit()
            if turn % 12 == 0:
                drain()
            result["snapshots"].append({"turn": turn, "context": asdict(ctx),
                "boundary_after_worker": db.get(ConversationSummary, c.id, populate_existing=True).summarized_until_message_id})
            print("TURN", turn, "API", len(provider.calls), flush=True)
            if turn in PROBES:
                probe(*[PROBES[turn][0], c, PROBES[turn][1]])
            save()
            if len(provider.calls) >= args.max_calls - 8:
                raise RuntimeError("reserved final probe budget reached")
        drain(retry=True)
        new = conversation(owner)
        service.load(db, new)
        drain()
        probe("N1", new, "我们最早聊到的睡眠困扰是什么原因，当时一般几点入睡？只回答这件事。")
        probe("N2", new, "列待办清单的效果究竟是谁的经历？请根据我最后的更正回答这一件事。")
        probe("N3", new, "晚饭后散步是我做过的吗，具体效果如何？")
        probe("N4", new, "我一直希望你采用什么交流方式？只回答这件事。")
        foreign = User(institutional_id="reliability_foreign", name="foreign", password_hash="unused", role="student")
        db.add(foreign)
        db.commit()
        other = conversation(foreign)
        first = message(other, "我为考试睡不好，折紫色风筝是我的睡前习惯。")
        reply = message(other, "了解。", role="assistant")
        service.enqueue_after_turn(db, other, first, reply)
        db.commit()
        drain()
        probe("N5", new, "你知道我有什么独特的睡前习惯吗？没有记录就说未找到，不要补写。")
        result["status"] = "completed"
    except Exception as exc:
        db.rollback()
        result["status"] = "incomplete"
        result["errors"].append({"type": type(exc).__name__, "message": str(exc)})
    finally:
        save()
        provider.inner.close()
        db.close()
        engine.dispose()
    print("RESULT", output / "result.json", result["status"], flush=True)
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
