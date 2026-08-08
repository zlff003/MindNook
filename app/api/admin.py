import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.dependencies import require_admin
from app.db import get_db
from app.models import (
    AlertDelivery,
    AgentCollaborationEvent,
    AgentRun,
    AgentRunTrace,
    AgentStep,
    AuditLog,
    BackgroundJob,
    CaseEvent,
    Message,
    PsychologicalReport,
    RiskCase,
    User,
)
from app.schemas import CaseNoteRequest, CaseTransitionRequest


router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

ALLOWED_CASE_TRANSITIONS = {
    "pending": {"notified", "assigned", "following", "closed"},
    "notified": {"assigned", "following", "closed"},
    "assigned": {"following", "closed"},
    "following": {"closed"},
    "closed": {"following"},
}


def _audit(
    db: Session,
    admin: User,
    action: str,
    resource_type: str,
    resource_public_id: str | None = None,
    detail: str = "",
) -> None:
    db.add(
        AuditLog(
            actor_id=admin.id,
            action=action,
            resource_type=resource_type,
            resource_public_id=resource_public_id,
            detail=detail,
        )
    )


@router.get("/dashboard/summary")
def dashboard_summary(
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    del admin
    return {
        "reports": db.scalar(
            select(func.count()).select_from(PsychologicalReport)
        )
        or 0,
        "high_risk": db.scalar(
            select(func.count())
            .select_from(PsychologicalReport)
            .where(PsychologicalReport.risk_level == "high")
        )
        or 0,
        "open_cases": db.scalar(
            select(func.count())
            .select_from(RiskCase)
            .where(RiskCase.status != "closed")
        )
        or 0,
        "failed_jobs": db.scalar(
            select(func.count())
            .select_from(BackgroundJob)
            .where(BackgroundJob.status == "DEAD")
        )
        or 0,
    }


@router.get("/reports")
def list_reports(
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    risk_level: str | None = None,
):
    del admin
    statement = (
        select(PsychologicalReport, User)
        .join(User, User.id == PsychologicalReport.student_id)
        .order_by(PsychologicalReport.id.desc())
    )
    if risk_level:
        statement = statement.where(PsychologicalReport.risk_level == risk_level)
    rows = db.execute(statement).all()
    return [
        {
            "id": report.public_id,
            "student": user.username,
            "dialogue_type": report.dialogue_type,
            "emotion_summary": report.emotion_summary,
            "risk_level": report.risk_level,
            "confidence": report.confidence,
            "reason_summary": report.reason_summary,
            "created_at": report.created_at,
            "generated_by_ai": report.generated_by_ai,
        }
        for report, user in rows
    ]


@router.get("/reports/{report_id}/context")
def report_context(
    report_id: str,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    report = db.scalar(
        select(PsychologicalReport).where(PsychologicalReport.public_id == report_id)
    )
    if not report:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="报告不存在。")
    source = db.get(Message, report.source_message_id)
    _audit(db, admin, "report_context_viewed", "report", report.public_id)
    db.commit()
    return {
        "report_id": report.public_id,
        "student_expression": source.content if source else "",
        "notice": "仅用于必要的人工跟进；本报告由智能系统生成，不构成临床诊断。",
    }


@router.get("/cases")
def list_cases(
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    del admin
    cases = list(db.scalars(select(RiskCase).order_by(RiskCase.id.desc())))
    return [
        {
            "id": item.public_id,
            "risk_level": item.risk_level,
            "status": item.status,
            "handoff_summary": item.handoff_summary,
            "note": item.note,
            "created_at": item.created_at,
        }
        for item in cases
    ]


def _case_or_404(db: Session, case_id: str) -> RiskCase:
    item = db.scalar(select(RiskCase).where(RiskCase.public_id == case_id))
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="个案不存在。")
    return item


@router.post("/cases/{case_id}/notes")
def add_case_note(
    case_id: str,
    payload: CaseNoteRequest,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    item = _case_or_404(db, case_id)
    item.note = payload.note.strip()
    db.add(
        CaseEvent(
            case_id=item.id,
            event_type="note_added",
            content=item.note,
            actor_id=admin.id,
        )
    )
    _audit(db, admin, "case_note_added", "case", item.public_id)
    db.commit()
    return {"id": item.public_id, "note": item.note, "status": item.status}


@router.post("/cases/{case_id}/transitions")
def transition_case(
    case_id: str,
    payload: CaseTransitionRequest,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    item = _case_or_404(db, case_id)
    old_status = item.status
    if payload.status == old_status:
        return {"id": item.public_id, "status": item.status}
    if payload.status not in ALLOWED_CASE_TRANSITIONS.get(old_status, set()):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"不允许从 {old_status} 变更为 {payload.status}。",
        )
    item.status = payload.status
    db.add(
        CaseEvent(
            case_id=item.id,
            event_type="status_changed",
            from_status=old_status,
            to_status=payload.status,
            actor_id=admin.id,
        )
    )
    _audit(
        db,
        admin,
        "case_status_changed",
        "case",
        item.public_id,
        detail=f"{old_status}->{payload.status}",
    )
    db.commit()
    return {"id": item.public_id, "status": item.status}


@router.get("/cases/{case_id}/events")
def list_case_events(
    case_id: str,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    item = _case_or_404(db, case_id)
    events = list(
        db.scalars(
            select(CaseEvent)
            .where(CaseEvent.case_id == item.id)
            .order_by(CaseEvent.id.asc())
        )
    )
    _audit(db, admin, "case_history_viewed", "case", item.public_id)
    db.commit()
    return [
        {
            "id": event.id,
            "event_type": event.event_type,
            "from_status": event.from_status,
            "to_status": event.to_status,
            "content": event.content,
            "created_at": event.created_at,
        }
        for event in events
    ]


@router.get("/alerts")
def list_alerts(
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    del admin
    alerts = list(db.scalars(select(AlertDelivery).order_by(AlertDelivery.id.desc())))
    return [
        {
            "id": item.id,
            "status": item.status,
            "channel": item.channel,
            "summary": item.summary,
            "created_at": item.created_at,
        }
        for item in alerts
    ]


@router.get("/jobs")
def list_jobs(
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    del admin
    jobs = list(db.scalars(select(BackgroundJob).order_by(BackgroundJob.id.desc())))
    return [
        {
            "id": item.public_id,
            "job_type": item.job_type,
            "status": item.status,
            "attempt_count": item.attempt_count,
            "max_attempts": item.max_attempts,
            "idempotency_key": item.idempotency_key,
            "depends_on_job_id": item.depends_on_job_id,
            "run_after": item.run_after,
            "last_error": item.last_error,
            "created_at": item.created_at,
        }
        for item in jobs
    ]


@router.get("/agent-runs")
def list_agent_runs(
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    del admin
    runs = list(db.scalars(select(AgentRun).order_by(AgentRun.id.desc()).limit(100)))
    return [
        {
            "id": item.public_id,
            "status": item.status,
            "route": item.route,
            "final_risk_level": item.final_risk_level,
            "provider": item.provider,
            "model_calls": item.model_calls,
            "degraded": item.degraded,
            "total_latency_ms": item.total_latency_ms,
            "created_at": item.created_at,
        }
        for item in runs
    ]


@router.get("/agent-runs/{run_id}/steps")
def list_agent_steps(
    run_id: str,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    run = db.scalar(select(AgentRun).where(AgentRun.public_id == run_id))
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="运行记录不存在。")
    steps = list(
        db.scalars(
            select(AgentStep)
            .where(AgentStep.run_id == run.id)
            .order_by(AgentStep.sequence.asc())
        )
    )
    _audit(db, admin, "agent_trace_viewed", "agent_run", run.public_id)
    db.commit()
    return [
        {
            "sequence": item.sequence,
            "step_name": item.step_name,
            "status": item.status,
            "model_name": item.model_name,
            "latency_ms": item.latency_ms,
            "input_tokens": item.input_tokens,
            "output_tokens": item.output_tokens,
            "summary": item.summary,
            "error_code": item.error_code,
        }
        for item in steps
    ]


@router.get("/agent-runs/{run_id}/events")
def list_agent_collaboration_events(
    run_id: str,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    run = db.scalar(select(AgentRun).where(AgentRun.public_id == run_id))
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="运行记录不存在。")
    events = list(
        db.scalars(
            select(AgentCollaborationEvent)
            .where(AgentCollaborationEvent.run_id == run.id)
            .order_by(AgentCollaborationEvent.sequence.asc())
        )
    )
    _audit(db, admin, "agent_collaboration_viewed", "agent_run", run.public_id)
    db.commit()
    return [
        {
            "sequence": item.sequence,
            "event_type": item.event_type,
            "actor": item.actor,
            "task_id": item.task_public_id,
            "payload": json.loads(item.payload_json),
        }
        for item in events
    ]


@router.get("/agent-runs/{run_id}/trace")
def get_agent_run_trace(
    run_id: str,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    run = db.scalar(select(AgentRun).where(AgentRun.public_id == run_id))
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="运行记录不存在。")
    trace = db.scalar(select(AgentRunTrace).where(AgentRunTrace.run_id == run.id))
    if not trace:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="审计记忆不存在。")
    _audit(db, admin, "agent_audit_memory_viewed", "agent_run", run.public_id)
    db.commit()
    return {
        "run_id": run.public_id,
        "source_input": trace.source_input,
        "redacted_input": trace.redacted_input,
        "memoryBrief": trace.memory_brief,
        "modelHistory": json.loads(trace.model_history_json),
        "agent_steps": json.loads(trace.agent_steps_json),
        "retrieval_results": json.loads(trace.retrieval_results_json),
        "assessment": json.loads(trace.assessment_json),
        "collaboration_events": json.loads(trace.collaboration_events_json),
        "tasks": json.loads(trace.tasks_json),
        "artifacts": json.loads(trace.artifacts_json),
        "messages": json.loads(trace.messages_json),
        "created_at": trace.created_at,
    }
