from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from sqlalchemy import func, select

from app.agents.runtime import CoordinatorAgent
from app.core.config import get_settings
from app.db import Base, build_engine, build_session_factory
from app.harness.agent_harness import AgentHarness, ChatCommand
from app.memory.private import AgentPrivateMemory
from app.memory.service import ShortTermMemoryService
from app.memory.store import build_redis_store
from app.memory.trace import AgentTraceService
from app.models import (
    AgentRun,
    AgentCollaborationEvent,
    AgentRunTrace,
    AgentStep,
    AlertDelivery,
    BackgroundJob,
    Conversation,
    LedgerEntry,
    Message,
    PsychologicalReport,
    RiskCase,
    User,
)
from app.services.model import build_model_provider
from app.services.tool_dispatcher import InlineToolDispatcher


SCENARIOS = {
    "general": "请用三句话解释 Python 列表推导式。",
    "support": "最近考试压力很大，晚上总是睡不着，我该怎么调整？",
    "high": "我想自杀，现在完全撑不住了。",
}


def _count(db, model) -> int:
    return db.scalar(select(func.count()).select_from(model)) or 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run one complete MindNook AgentHarness scenario without HTTP"
    )
    parser.add_argument("--scenario", choices=SCENARIOS, default="support")
    parser.add_argument(
        "--require-model",
        action="store_true",
        help="Return a failure code if any model step falls back.",
    )
    args = parser.parse_args()

    base_settings = get_settings()
    with tempfile.TemporaryDirectory(prefix="mindnook-harness-") as temp_dir:
        database_path = Path(temp_dir) / "harness.db"
        excel_path = Path(temp_dir) / "risk-ledger.xlsx"
        settings = base_settings.model_copy(
            update={
                "database_url": f"sqlite:///{database_path.as_posix()}",
                "excel_ledger_path": str(excel_path),
            }
        )
        engine = build_engine(settings.database_url)
        Base.metadata.create_all(engine)
        session_factory = build_session_factory(engine)
        provider = build_model_provider(settings)
        redis_store = build_redis_store(settings)
        dispatcher = InlineToolDispatcher(session_factory, settings)
        dispatcher.start()
        try:
            db = session_factory()
            try:
                student = User(
                    username="harness-student",
                    display_name="Harness Student",
                    password_hash="not-used-by-harness",
                    role="student",
                    status="active",
                )
                db.add(student)
                db.flush()
                conversation = Conversation(student_id=student.id)
                db.add(conversation)
                db.commit()
                student_public_id = student.public_id
                conversation_public_id = conversation.public_id
            finally:
                db.close()

            runtime = CoordinatorAgent(provider, settings)
            harness = AgentHarness(
                session_factory,
                runtime,
                dispatcher,
                ShortTermMemoryService(settings, redis_store),
                AgentPrivateMemory(settings, redis_store),
                AgentTraceService(),
            )
            events = list(
                harness.run(
                    ChatCommand(
                        student_public_id=student_public_id,
                        conversation_public_id=conversation_public_id,
                        content=SCENARIOS[args.scenario],
                        client_request_id=f"harness-{args.scenario}-0001",
                    )
                )
            )

            db = session_factory()
            try:
                run = db.scalar(select(AgentRun).order_by(AgentRun.id.desc()))
                steps = (
                    list(
                        db.scalars(
                            select(AgentStep)
                            .where(AgentStep.run_id == run.id)
                            .order_by(AgentStep.sequence)
                        )
                    )
                    if run
                    else []
                )
                failed_model_steps = [step.step_name for step in steps if step.status == "failed"]
                error_events = [event.payload for event in events if event.type == "error"]
                output = {
                    "ok": bool(run) and not error_events and not failed_model_steps,
                    "scenario": args.scenario,
                    "provider": run.provider if run else settings.model_provider,
                    "route": run.route if run else None,
                    "risk_level": run.final_risk_level if run else None,
                    "model_calls": run.model_calls if run else 0,
                    "degraded": run.degraded if run else True,
                    "total_latency_ms": run.total_latency_ms if run else 0,
                    "public_events": [event.type for event in events if event.public],
                    "internal_events": [event.type for event in events if not event.public],
                    "records": {
                        "messages": _count(db, Message),
                        "agent_runs": _count(db, AgentRun),
                        "collaboration_events": _count(db, AgentCollaborationEvent),
                        "agent_run_traces": _count(db, AgentRunTrace),
                        "reports": _count(db, PsychologicalReport),
                        "jobs": _count(db, BackgroundJob),
                        "ledger_entries": _count(db, LedgerEntry),
                        "risk_cases": _count(db, RiskCase),
                        "alerts": _count(db, AlertDelivery),
                        "excel_exists": excel_path.is_file(),
                    },
                    "steps": [
                        {
                            "name": step.step_name,
                            "status": step.status,
                            "model": step.model_name,
                            "latency_ms": step.latency_ms,
                            "input_tokens": step.input_tokens,
                            "output_tokens": step.output_tokens,
                            "error_code": step.error_code,
                        }
                        for step in steps
                    ],
                    "errors": error_events,
                }
            finally:
                db.close()

            print(json.dumps(output, ensure_ascii=False, indent=2))
            return 1 if args.require_model and not output["ok"] else 0
        finally:
            dispatcher.close()
            close_provider = getattr(provider, "close", None)
            if callable(close_provider):
                close_provider()
            close_redis = getattr(redis_store, "close", None)
            if callable(close_redis):
                close_redis()
            engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
