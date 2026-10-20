"""Opt-in atomic-question controls and, if capped, the missing short-session probe."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.agents.prompts import RESPONSE_SYSTEM_PROMPT
from app.agents.runtime import RuntimeInput
from app.core.config import Settings
from app.memory.types import MemoryContext, MemoryMessage
from app.services.model import ModelMessage, build_model_provider


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("run_id")
    parser.add_argument("--live",action="store_true")
    args=parser.parse_args()
    if not args.live:
        parser.error("--live required; at most four paid logical API calls")
    if Path(args.run_id).name!=args.run_id or args.run_id in {".",".."}:
        parser.error("run_id must be one directory name")
    directory=ROOT/"tmp"/"memory-live"/args.run_id
    result=json.loads((directory/"result.json").read_text(encoding="utf-8"))
    target=directory/"controls.json"
    if target.exists():
        raise RuntimeError("controls already exist; refusing accidental repeat billing")
    settings=Settings()
    if settings.model_provider=="mock" or not settings.model_api_key:
        raise RuntimeError("real API configuration required")
    from memory_live_eval import RecordingProvider
    extra=directory/"controls"
    extra.mkdir(exist_ok=False)
    provider=RecordingProvider(build_model_provider(settings),extra,limit=4)
    records=[]
    for source,question in [
        ("P4","我们最初聊睡眠时，我说自己通常几点才能入睡？只回答这一件事，没有确切记录就说明不知道。"),
        ("P6","我最后一次明确更正后，列待办的执行和效果应该算在谁的经历里？只回答这一件事，没有确切记录就说明不知道。"),
    ]:
        probe=next((p for p in result["probes"] if p["id"]==source),None)
        if probe is None:
            records.append(dict(source=source,status="not_run",reason="source checkpoint missing"))
            continue
        data=dict(probe["context"])
        data["model_history"]=tuple(MemoryMessage(**item) for item in data["model_history"])
        data["recalled_memories"]=tuple(data["recalled_memories"])
        ctx=MemoryContext(**data)
        messages=[ModelMessage("system",RESPONSE_SYSTEM_PROMPT),*RuntimeInput(question,ctx).history_prompt_messages,ModelMessage("user",question)]
        record=dict(source=source,question=question,messages=[asdict(m) for m in messages])
        started=time.monotonic()
        try:
            response=provider.complete(messages,model=settings.model_response_name or settings.model_name,
                temperature=0,json_mode=False,purpose="response_generation")
            record.update(status="ok",response=asdict(response))
        except Exception as exc:
            record.update(status="error",error=type(exc).__name__,message=str(exc),code=getattr(exc,"code",None))
        record["elapsed_seconds"]=round(time.monotonic()-started,3)
        records.append(record)
        target.write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding="utf-8")
        print(source,record["status"],flush=True)
    short=next((p for p in result["probes"] if p["id"]=="P8"),None)
    if short is not None and not short.get("answer"):
        from app.db import build_engine, build_session_factory
        from app.memory.index import MemoryIndex
        from app.memory.service import MemoryService
        from app.memory.store import InMemoryRedisStore
        from app.models import Conversation
        isolated=settings.model_copy(update={"app_env":"memory_live",
            "database_url":f"sqlite:///{(directory/'memory.db').as_posix()}",
            "rag_chroma_path":str(directory/"chroma"),"memory_collection_name":"mindnook_live_evaluation"})
        engine=build_engine(isolated.database_url)
        db=build_session_factory(engine)()
        service=MemoryService(isolated,InMemoryRedisStore(),provider,index=MemoryIndex(isolated))
        record=dict(source="P8_followup",question=short["question"])
        try:
            conv=db.get(Conversation,short["conversation_id"])
            ctx=service.load(db,conv,current_input=short["question"])
            record["context"]=asdict(ctx)
            messages=[ModelMessage("system",RESPONSE_SYSTEM_PROMPT),
                *RuntimeInput(short["question"],ctx).history_prompt_messages,ModelMessage("user",short["question"])]
            response=provider.complete(messages,model=settings.model_response_name or settings.model_name,
                temperature=0,json_mode=False,purpose="response_generation")
            record.update(status="ok",response=asdict(response))
        except Exception as exc:
            record.update(status="error",error=type(exc).__name__,message=str(exc))
        finally:
            db.close(); engine.dispose()
        records.append(record)
        target.write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding="utf-8")
    provider.inner.close()


if __name__=="__main__":
    main()
