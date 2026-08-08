from __future__ import annotations

import json
import socket
import threading
import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.core.config import Settings
from app.models import (
    DeadLetterRecord,
    PsychologicalReport,
    ToolJob,
    utc_now,
)
from app.tools.operations import ToolOperations


EXCEL_REPORT = "EXCEL_REPORT"
CASE_CREATE = "CASE_CREATE"
ALERT_SEND = "ALERT_SEND"
CASE_NOTE_ADD = "CASE_NOTE_ADD"
ALERT_ACK = "ALERT_ACK"
ALERT_NOTIFY = "ALERT_NOTIFY"

PENDING = "PENDING"
RUNNING = "RUNNING"
SUCCESS = "SUCCESS"
DEAD = "DEAD"

EXCEL_JOB_TYPES = {EXCEL_REPORT}
CASE_JOB_TYPES = {CASE_CREATE, CASE_NOTE_ADD, ALERT_ACK}
ALERT_JOB_TYPES = {ALERT_SEND, ALERT_NOTIFY}


def _payload(**values) -> str:
    return json.dumps(values, ensure_ascii=False, sort_keys=True)


def enqueue_tool_job(
    db,
    *,
    job_type: str,
    idempotency_key: str,
    payload: dict,
    priority: int = 10,
    report_id: int | None = None,
    depends_on_job_id: int | None = None,
    max_attempts: int = 3,
) -> ToolJob:
    """Create one durable job or return the existing idempotent job."""
    existing = db.scalar(
        select(ToolJob).where(ToolJob.idempotency_key == idempotency_key)
    )
    if existing:
        return existing
    values = {
        "public_id": str(uuid.uuid4()),
        "job_type": job_type,
        "idempotency_key": idempotency_key,
        "report_id": report_id,
        "depends_on_job_id": depends_on_job_id,
        "payload_json": json.dumps(payload, ensure_ascii=False, sort_keys=True),
        "priority": priority,
        "max_attempts": max(1, max_attempts),
        "status": PENDING,
        "attempts": 0,
        "run_after": utc_now(),
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }
    dialect = db.get_bind().dialect.name
    if dialect == "mysql":
        statement = mysql_insert(ToolJob.__table__).values(**values)
        statement = statement.on_duplicate_key_update(
            idempotency_key=statement.inserted.idempotency_key
        )
        db.execute(statement)
    elif dialect == "sqlite":
        statement = sqlite_insert(ToolJob.__table__).values(
            **values
        ).on_conflict_do_nothing(
            index_elements=["idempotency_key"]
        )
        db.execute(statement)
    else:
        job = ToolJob(
            **{
                key: value
                for key, value in values.items()
                if key not in {"attempts"}
            },
            attempt_count=0,
        )
        db.add(job)
        db.flush()
        return job
    job = db.scalar(
        select(ToolJob).where(ToolJob.idempotency_key == idempotency_key)
    )
    if not job:
        raise RuntimeError(f"failed to create idempotent job: {idempotency_key}")
    return job


def enqueue_report_jobs(
    db,
    report: PsychologicalReport,
    tool_plan: list[str],
    *,
    max_attempts: int = 3,
) -> list[ToolJob]:
    """Persist the risk-level tool DAG selected by AgentHarness."""
    requested = set(tool_plan)
    jobs: list[ToolJob] = []
    excel_job: ToolJob | None = None
    case_job: ToolJob | None = None
    if "ledger_write" in requested:
        excel_job = enqueue_tool_job(
            db,
            job_type=EXCEL_REPORT,
            idempotency_key=f"excel_report:{report.public_id}",
            payload={"report_id": report.public_id},
            report_id=report.id,
            priority=50,
            max_attempts=max_attempts,
        )
        jobs.append(excel_job)
    if "case_create" in requested:
        case_job = enqueue_tool_job(
            db,
            job_type=CASE_CREATE,
            idempotency_key=f"case_create:{report.public_id}",
            payload={"report_id": report.public_id},
            report_id=report.id,
            priority=100,
            max_attempts=max_attempts,
        )
        jobs.append(case_job)
    if "alert_send" in requested:
        if not case_job:
            raise ValueError("alert_send requires case_create")
        alert_job = enqueue_tool_job(
            db,
            job_type=ALERT_SEND,
            idempotency_key=f"alert_send:{report.public_id}",
            payload={"report_id": report.public_id},
            report_id=report.id,
            depends_on_job_id=case_job.id,
            priority=90,
            max_attempts=max_attempts,
        )
        jobs.append(alert_job)
    unsupported = requested - {"ledger_write", "case_create", "alert_send"}
    if unsupported:
        raise ValueError(f"unsupported tool plan items: {sorted(unsupported)}")
    return jobs


def execute_tool_job(job: ToolJob, operations: ToolOperations) -> dict:
    payload = json.loads(job.payload_json or "{}")
    if job.job_type == EXCEL_REPORT:
        return operations.excel_report(payload["report_id"])
    if job.job_type == CASE_CREATE:
        return operations.case_create(payload["report_id"])
    if job.job_type == ALERT_SEND:
        return operations.alert_send(payload["report_id"])
    if job.job_type == CASE_NOTE_ADD:
        return operations.case_note_add(
            payload["case_id"],
            payload["note"],
            actor_username=payload.get("actor_username", "queue"),
        )
    if job.job_type == ALERT_ACK:
        return operations.alert_ack(
            int(payload["alert_id"]),
            actor_username=payload.get("actor_username", "queue"),
            note=payload.get("note", ""),
        )
    if job.job_type == ALERT_NOTIFY:
        return operations.alert_notify(int(payload["alert_id"]))
    raise RuntimeError(f"unsupported tool job type: {job.job_type}")


def worker_identity() -> str:
    return f"{socket.gethostname()}:{threading.get_native_id()}:{uuid.uuid4().hex[:8]}"


def recover_stale_jobs(
    session_factory,
    timeout_seconds: int,
    *,
    recover_all: bool = False,
) -> int:
    db = session_factory()
    try:
        cutoff = utc_now() - timedelta(seconds=max(1, timeout_seconds))
        statement = select(ToolJob).where(ToolJob.status == RUNNING)
        if not recover_all:
            statement = statement.where(
                (ToolJob.locked_at.is_(None)) | (ToolJob.locked_at <= cutoff)
            )
        jobs = list(db.scalars(statement))
        for job in jobs:
            job.status = PENDING
            job.locked_by = None
            job.locked_at = None
            job.run_after = utc_now()
            job.last_error = "worker restarted or running lease expired"
        db.commit()
        return len(jobs)
    finally:
        db.close()


def _dead_letter(db, job: ToolJob, error: str) -> None:
    job.status = DEAD
    job.last_error = error[:1000]
    job.locked_by = None
    job.locked_at = None
    existing = db.scalar(
        select(DeadLetterRecord).where(DeadLetterRecord.job_id == job.id)
    )
    if not existing:
        db.add(
            DeadLetterRecord(
                job_id=job.id,
                job_public_id=job.public_id,
                job_type=job.job_type,
                idempotency_key=job.idempotency_key,
                payload_json=job.payload_json,
                attempts=job.attempt_count,
                last_error=job.last_error,
            )
        )


def claim_due_jobs(
    session_factory,
    *,
    worker_id: str,
    batch_size: int,
) -> list[int]:
    db = session_factory()
    try:
        statement = (
            select(ToolJob)
            .where(ToolJob.status == PENDING, ToolJob.run_after <= utc_now())
            .order_by(ToolJob.priority.desc(), ToolJob.id.asc())
            .limit(max(1, batch_size))
            .with_for_update(skip_locked=True)
        )
        candidates = list(db.scalars(statement))
        claimed: list[int] = []
        for job in candidates:
            if job.depends_on_job_id:
                dependency = db.get(ToolJob, job.depends_on_job_id)
                if not dependency:
                    _dead_letter(db, job, "dependency job is missing")
                    continue
                if dependency.status == DEAD:
                    _dead_letter(db, job, "dependency job entered DEAD")
                    continue
                if dependency.status != SUCCESS:
                    continue
            job.status = RUNNING
            job.attempt_count += 1
            job.locked_by = worker_id
            job.locked_at = utc_now()
            claimed.append(job.id)
        db.commit()
        return claimed
    finally:
        db.close()


def mark_job_success(session_factory, job_id: int) -> None:
    db = session_factory()
    try:
        job = db.get(ToolJob, job_id)
        if job:
            job.status = SUCCESS
            job.last_error = None
            job.locked_by = None
            job.locked_at = None
            db.commit()
    finally:
        db.close()


def reschedule_rate_limited_job(
    session_factory,
    job_id: int,
    delay_seconds: float,
) -> None:
    db = session_factory()
    try:
        job = db.get(ToolJob, job_id)
        if job:
            job.status = PENDING
            job.attempt_count = max(0, job.attempt_count - 1)
            job.run_after = utc_now() + timedelta(seconds=max(0.1, delay_seconds))
            job.locked_by = None
            job.locked_at = None
            job.last_error = "deferred by alert rate limiter"
            db.commit()
    finally:
        db.close()


def mark_job_failure(
    session_factory,
    job_id: int,
    error: str,
    *,
    retry_base_seconds: float,
) -> None:
    db = session_factory()
    try:
        job = db.get(ToolJob, job_id)
        if not job:
            return
        if job.attempt_count >= job.max_attempts:
            _dead_letter(db, job, error)
        else:
            delay = max(0.1, retry_base_seconds) * (2 ** max(0, job.attempt_count - 1))
            job.status = PENDING
            job.run_after = utc_now() + timedelta(seconds=delay)
            job.last_error = error[:1000]
            job.locked_by = None
            job.locked_at = None
        db.commit()
    finally:
        db.close()


def process_pending_jobs(session_factory, settings: Settings) -> None:
    """Synchronous deterministic drain used by tests and the standalone Harness."""
    operations = ToolOperations(session_factory, settings)
    worker_id = worker_identity()
    while True:
        job_ids = claim_due_jobs(
            session_factory,
            worker_id=worker_id,
            batch_size=settings.tool_worker_batch_size,
        )
        if not job_ids:
            break
        for job_id in job_ids:
            db = session_factory()
            try:
                job = db.get(ToolJob, job_id)
                if not job:
                    continue
                execute_tool_job(job, operations)
                mark_job_success(session_factory, job_id)
            except Exception as exc:  # noqa: BLE001
                mark_job_failure(
                    session_factory,
                    job_id,
                    str(exc),
                    retry_base_seconds=settings.tool_worker_retry_base_seconds,
                )
            finally:
                db.close()
