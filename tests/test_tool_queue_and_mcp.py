import asyncio
import os
import tempfile
import time
from pathlib import Path

from sqlalchemy import func, select

from app.core.config import Settings
from app.db import Base, build_engine, build_session_factory
from app.mcp.client import run_report_tools
from app.mcp.server import mcp
from app.models import (
    AlertDelivery,
    Conversation,
    DeadLetterRecord,
    LedgerEntry,
    Message,
    PsychologicalReport,
    RiskCase,
    ToolJob,
    User,
    utc_now,
)
from app.services.jobs import (
    DEAD,
    EXCEL_REPORT,
    RUNNING,
    SUCCESS,
    enqueue_report_jobs,
    enqueue_tool_job,
    process_pending_jobs,
    recover_stale_jobs,
)
from app.services.tool_dispatcher import (
    InlineToolDispatcher,
    ThreadedToolDispatcher,
    _PerMinuteRateLimiter,
)


def _count(db, model) -> int:
    return db.scalar(select(func.count()).select_from(model)) or 0


class TestToolQueueAndMcp:
    def setup_method(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "tool-queue.db"
        self.excel_path = Path(self.temp_dir.name) / "risk-ledger.xlsx"
        self.database_url = f"sqlite:///{self.database_path.as_posix()}"
        self.settings = Settings(
            app_env="test",
            database_url=self.database_url,
            excel_ledger_path=str(self.excel_path),
            tool_worker_poll_seconds=0.05,
            tool_worker_retry_base_seconds=0.01,
        )
        self.engine = build_engine(self.database_url)
        Base.metadata.create_all(self.engine)
        self.session_factory = build_session_factory(self.engine)

    def teardown_method(self):
        self.engine.dispose()
        self.temp_dir.cleanup()

    def _report(self, risk_level="RISK") -> PsychologicalReport:
        db = self.session_factory()
        try:
            user = User(
                username=f"student-{time.time_ns()}",
                display_name="Queue Student",
                password_hash="unused",
                role="student",
            )
            db.add(user)
            db.flush()
            conversation = Conversation(student_id=user.id)
            db.add(conversation)
            db.flush()
            source = Message(
                conversation_id=conversation.id,
                sender_type="student",
                content="I need help",
                client_request_id=f"queue-{time.time_ns()}",
            )
            db.add(source)
            db.flush()
            report = PsychologicalReport(
                conversation_id=conversation.id,
                source_message_id=source.id,
                student_id=user.id,
                emotion_summary="distressed",
                risk_level=risk_level,
                confidence=0.95,
                reason_summary="requires a human follow-up",
            )
            db.add(report)
            db.commit()
            return report
        finally:
            db.close()

    def test_high_risk_dag_is_idempotent_and_executes_in_dependency_order(self):
        report = self._report()
        db = self.session_factory()
        try:
            first = enqueue_report_jobs(
                db,
                report,
                ["ledger_write", "case_create", "alert_send"],
            )
            second = enqueue_report_jobs(
                db,
                report,
                ["ledger_write", "case_create", "alert_send"],
            )
            db.commit()
            assert [job.id for job in first] == [job.id for job in second]
            assert first[2].depends_on_job_id == first[1].id
            assert _count(db, ToolJob) == 3
        finally:
            db.close()

        dispatcher = InlineToolDispatcher(self.session_factory, self.settings)
        dispatcher.dispatch()
        db = self.session_factory()
        try:
            jobs = list(db.scalars(select(ToolJob).order_by(ToolJob.id)))
            assert {job.status for job in jobs} == {SUCCESS}
            assert all(job.attempt_count == 1 for job in jobs)
            assert _count(db, LedgerEntry) == 1
            assert _count(db, RiskCase) == 1
            assert _count(db, AlertDelivery) == 1
            assert self.excel_path.is_file()
        finally:
            db.close()

    def test_permanent_failure_is_recorded_once_in_dead_letter_table(self):
        db = self.session_factory()
        try:
            enqueue_tool_job(
                db,
                job_type="UNSUPPORTED_TEST_JOB",
                idempotency_key="unsupported:test",
                payload={},
                max_attempts=1,
            )
            db.commit()
        finally:
            db.close()

        process_pending_jobs(self.session_factory, self.settings)
        db = self.session_factory()
        try:
            job = db.scalar(select(ToolJob))
            dead = db.scalar(select(DeadLetterRecord))
            assert job.status == DEAD
            assert job.attempt_count == 1
            assert dead.job_id == job.id
            assert "unsupported tool job type" in dead.last_error
        finally:
            db.close()

    def test_startup_recovery_requeues_running_jobs(self):
        db = self.session_factory()
        try:
            job = enqueue_tool_job(
                db,
                job_type=EXCEL_REPORT,
                idempotency_key="recover:test",
                payload={"report_id": "missing"},
            )
            job.status = RUNNING
            job.locked_by = "dead-worker"
            db.commit()
        finally:
            db.close()
        assert recover_stale_jobs(
            self.session_factory,
            300,
            recover_all=True,
        ) == 1
        db = self.session_factory()
        try:
            job = db.scalar(select(ToolJob))
            assert job.status == "PENDING"
            assert job.locked_by is None
        finally:
            db.close()

    def test_failure_retries_with_delay_before_dead_letter(self):
        db = self.session_factory()
        try:
            enqueue_tool_job(
                db,
                job_type="UNSUPPORTED_RETRY_JOB",
                idempotency_key="retry:test",
                payload={},
                max_attempts=3,
            )
            db.commit()
        finally:
            db.close()
        for expected_attempt in (1, 2, 3):
            process_pending_jobs(self.session_factory, self.settings)
            db = self.session_factory()
            try:
                job = db.scalar(select(ToolJob))
                assert job.attempt_count == expected_attempt
                if expected_attempt < 3:
                    assert job.status == "PENDING"
                    assert job.run_after is not None
                    assert job.last_error
                    job.run_after = utc_now()
                    db.commit()
                else:
                    assert job.status == DEAD
                    assert _count(db, DeadLetterRecord) == 1
            finally:
                db.close()

    def test_alert_rate_limiter_reserves_only_configured_slots(self):
        limiter = _PerMinuteRateLimiter(1)
        assert limiter.acquire_or_delay() == 0
        assert limiter.acquire_or_delay() > 59

    def test_threaded_dispatch_is_non_blocking(self):
        report = self._report("CONSULT")
        db = self.session_factory()
        try:
            enqueue_report_jobs(db, report, ["ledger_write"])
            db.commit()
        finally:
            db.close()

        dispatcher = ThreadedToolDispatcher(self.session_factory, self.settings)
        original = dispatcher.operations.excel_report

        def slow_excel(report_id):
            time.sleep(0.25)
            return original(report_id)

        dispatcher.operations.excel_report = slow_excel
        dispatcher.start()
        started = time.monotonic()
        dispatcher.dispatch()
        elapsed = time.monotonic() - started
        assert elapsed < 0.05
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            db = self.session_factory()
            try:
                status = db.scalar(select(ToolJob.status))
            finally:
                db.close()
            if status == SUCCESS:
                break
            time.sleep(0.05)
        dispatcher.close()
        assert status == SUCCESS

    def test_fastmcp_registers_six_tools_and_stdio_client_runs_high_risk_chain(self):
        tools = asyncio.run(mcp.list_tools())
        assert {tool.name for tool in tools} == {
            "mindbridge_excel_report",
            "mindbridge_case_create",
            "mindbridge_alert_send",
            "mindbridge_alert_ack",
            "mindbridge_case_note_add",
            "mindbridge_alert_notify",
        }
        report = self._report()
        child_env = dict(os.environ)
        child_env["DATABASE_URL"] = self.database_url
        child_env["EXCEL_LEDGER_PATH"] = str(self.excel_path)
        outputs = asyncio.run(
            run_report_tools(report.public_id, "RISK", env=child_env)
        )
        assert [item["tool"] for item in outputs] == [
            "mindbridge_excel_report",
            "mindbridge_case_create",
            "mindbridge_alert_send",
        ]
        db = self.session_factory()
        try:
            assert _count(db, LedgerEntry) == 1
            assert _count(db, RiskCase) == 1
            assert _count(db, AlertDelivery) == 1
        finally:
            db.close()
