"""Prepare a versioned SQLite shadow rebuild. Never switches or writes the source DB."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import delete, select
from app.db import Base, build_engine, build_session_factory
from app.models import Conversation, ConversationSummary, MemoryBatch, MemoryEvent, ToolJob, UserMemory


def prepare_shadow(source: Path, output: Path):
    source, output = source.resolve(strict=True), output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    # The immutable source snapshot retains old facts, versions, events and summaries.
    snapshot, target = output / "source_snapshot.db", output / "memory_rebuilt.db"
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original:
        with closing(sqlite3.connect(snapshot)) as backup:
            original.backup(backup)
    with closing(sqlite3.connect(snapshot)) as backup:
        with closing(sqlite3.connect(target)) as shadow:
            backup.backup(shadow)
    engine = build_engine(f"sqlite:///{target.as_posix()}")
    Base.metadata.create_all(engine)
    from app.memory.schema import migrate_memory_schema
    migrate_memory_schema(engine)
    factory = build_session_factory(engine)
    with factory() as db:
        # Only disposable projections in the newly created shadow are reset.
        for model in (MemoryEvent, MemoryBatch, UserMemory, ConversationSummary):
            db.execute(delete(model))
        for job in db.scalars(select(ToolJob).where(ToolJob.job_type.like("MEMORY_%"))):
            job.status = "SUCCESS"
            job.idempotency_key = f"shadow-archived-job:{job.id}"
            job.locked_at = job.locked_by = None
        db.commit()
    engine.dispose()
    manifest = {"source": str(source), "snapshot": str(snapshot), "shadow": str(target),
                "status": "prepared", "activated": False,
                "note": "No model calls during preparation. Original source is unchanged."}
    (output / "rebuild.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path, help="Existing SQLite database, opened read-only")
    parser.add_argument("--output", required=True, type=Path, help="New isolated directory")
    parser.add_argument("--live", action="store_true", help="Opt in to actual model extraction of the copied messages")
    parser.add_argument("--max-calls", type=int, default=100)
    args = parser.parse_args()
    if not 1 <= args.max_calls <= 1000:
        parser.error("max-calls must be 1..1000")
    manifest = prepare_shadow(args.source, args.output)
    if not args.live:
        print(json.dumps(manifest, ensure_ascii=True))
        return 0
    from app.core.config import Settings
    from app.memory.repository import ensure_summary, snapshot_upper
    from app.memory.service import MemoryService
    from app.memory.store import InMemoryRedisStore
    from app.services.model import build_model_provider
    from scripts.memory_live_eval import RecordingProvider
    settings = Settings().model_copy(update={"app_env": "memory_rebuild",
        "database_url": f"sqlite:///{Path(manifest['shadow']).as_posix()}",
        "rag_chroma_path": str(args.output.resolve() / "chroma"),
        "memory_collection_name": "memory_rebuilt_v2", "model_max_retries": 0})
    if not settings.model_api_key or settings.model_provider == "mock":
        raise RuntimeError("real model configuration required for --live")
    engine = build_engine(settings.database_url)
    db = build_session_factory(engine)()
    provider = RecordingProvider(build_model_provider(settings), args.output, limit=args.max_calls)
    service = MemoryService(settings, InMemoryRedisStore(), provider)
    try:
        for conversation in list(db.scalars(select(Conversation).order_by(Conversation.id))):
            while ensure_summary(db, conversation.id).extracted_until_message_id < snapshot_upper(db, conversation.id):
                service.extract_pending(db, conversation, background=True)
        # No non-memory business jobs are executed from a copied production database.
        for kind in ("MEMORY_RECONCILE", "MEMORY_INDEX_SYNC"):
            for job in list(db.scalars(select(ToolJob).where(ToolJob.job_type == kind, ToolJob.status == "PENDING"))):
                service.execute_job(db, job)
                job.status = "SUCCESS"
                db.commit()
        manifest["status"] = "rebuilt_pending_review"
    except Exception as exc:
        db.rollback()
        manifest.update(status="incomplete", error=str(exc))
    finally:
        manifest["api_calls"] = len(provider.calls)
        (args.output / "rebuild.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        provider.inner.close()
        db.close()
        engine.dispose()
    print(json.dumps(manifest, ensure_ascii=True))
    return 0 if manifest["status"] == "rebuilt_pending_review" else 1


if __name__ == "__main__":
    raise SystemExit(main())
