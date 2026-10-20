"""Bounded post-60-turn regression probes on a copy of a completed live run."""
import argparse
import json
import shutil
import sqlite3
import sys
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sqlalchemy import select
from app.agents.prompts import RESPONSE_SYSTEM_PROMPT
from app.agents.runtime import RuntimeInput
from app.core.config import Settings
from app.db import build_engine, build_session_factory
from app.memory.service import MemoryService
from app.memory.store import InMemoryRedisStore
from app.models import Conversation, User
from app.services.model import ModelMessage, build_model_provider
from scripts.memory_live_eval import RecordingProvider, write_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("run_id")
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if not args.live or Path(args.run_id).name != args.run_id or args.run_id in (".", ".."):
        parser.error("--live and one completed run-id are required; this makes at most 4 API calls")
    source = ROOT / "tmp" / "memory-live" / args.run_id
    baseline = json.loads((source / "result.json").read_text(encoding="utf-8"))
    if baseline["status"] != "completed":
        raise RuntimeError("source run is incomplete")
    output = source / "followup-v4"
    output.mkdir(exist_ok=False)
    with closing(sqlite3.connect((source / "memory.db").as_uri() + "?mode=ro", uri=True)) as original:
        with closing(sqlite3.connect(output / "memory.db")) as target:
            original.backup(target)
    shutil.copytree(source / "chroma", output / "chroma")
    settings = Settings().model_copy(update={"app_env": "memory_live", "model_max_retries": 0,
        "database_url": f"sqlite:///{(output / 'memory.db').as_posix()}",
        "rag_chroma_path": str(output / "chroma"), "memory_collection_name": "memory_reliability_v2",
        "memory_foreground_model_calls": False})
    if settings.model_provider == "mock" or not settings.model_api_key:
        raise RuntimeError("real model configuration is required")
    engine = build_engine(settings.database_url)
    db = build_session_factory(engine)()
    provider = RecordingProvider(build_model_provider(settings), output, limit=4)
    service = MemoryService(settings, InMemoryRedisStore(), provider)
    owner = db.scalar(select(User).where(User.institutional_id == "reliability_owner"))
    conversation = Conversation(student_id=owner.id)
    db.add(conversation)
    db.commit()
    questions = [
        ("F1", "我们最早聊的睡眠困扰是什么原因、通常几点入睡？我长期希望你用什么方式和我交流？换话题是否代表之前的问题已经解决？请回答这几个已有事实，不另提问题。"),
        ("F2", "根据我最后的更正，清单有效是谁的经历？散步是不是我本人做的？"),
        ("F3", "我说今天不想听建议，是我一直拒绝建议的固定偏好吗？我真正长期希望的交流方式是什么？"),
        ("F4", "我最初一般几点才睡？请只根据最早的明确记录回答，未找到就说明未找到。"),
    ]
    result = {"source_run": args.run_id, "scope": "Post-60-turn copied database; no extraction or worker replay", "probes": []}
    try:
        for key, question in questions:
            ctx = service.load(db, conversation, current_input=question)
            item = {"id": key, "question": question, "context": asdict(ctx)}
            try:
                response = provider.complete([ModelMessage("system", RESPONSE_SYSTEM_PROMPT),
                    *RuntimeInput(question, ctx).history_prompt_messages, ModelMessage("user", question)],
                    model=settings.model_response_name or settings.model_name, temperature=0,
                    json_mode=False, purpose="response_generation")
                item["answer"] = response.content
            except Exception as exc:
                item["error"] = str(exc)
            result["probes"].append(item)
            write_json(output / "result.json", result)
    finally:
        provider.inner.close()
        db.close()
        engine.dispose()
    print(output / "result.json")
    return 0 if all("answer" in p for p in result["probes"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
