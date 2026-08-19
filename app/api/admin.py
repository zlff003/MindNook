import json
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.api.dependencies import require_admin
from app.core.security import hash_password
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
from app.schemas import (
    AccountRegistrationRequest,
    CaseAssignmentRequest,
    CaseNoteRequest,
    CaseTransitionRequest,
)
from app.services.excel_exports import build_excel_export
from app.skills.registry import SkillRegistry


router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

ALLOWED_CASE_TRANSITIONS = {
    "pending": {"notified", "assigned", "following", "closed"},
    "notified": {"assigned", "following", "closed"},
    "assigned": {"following", "closed"},
    "following": {"closed"},
    "closed": {"following"},
}

AUDIT_ACTION_LABELS = {
    "student_registered": "学生注册账号",
    "admin_account_created": "创建管理员账号",
    "user_list_viewed": "查看用户列表",
    "report_context_viewed": "查看报告敏感原文",
    "case_list_viewed": "查看个案列表",
    "case_owner_assigned": "分配或转交个案",
    "case_note_added": "添加个案备注",
    "case_status_changed": "变更个案状态",
    "case_history_viewed": "查看个案处理记录",
    "audit_log_viewed": "查看审计日志",
    "excel_exported": "导出 Excel",
    "agent_trace_viewed": "查看 Agent 执行步骤",
    "agent_collaboration_viewed": "查看 Agent 协作事件",
    "agent_audit_memory_viewed": "查看 Agent 审计轨迹",
}

AUDIT_RESOURCE_LABELS = {
    "user": "用户账号",
    "report": "心理支持报告",
    "case": "风险个案",
    "audit_log": "审计日志",
    "agent_run": "Agent 运行",
    "reports": "报告导出",
    "cases": "个案导出",
    "audit-logs": "审计日志导出",
}

AUDIT_DETAIL_KEY_LABELS = {
    "institutional_id": "学号/工号",
    "owner": "负责人",
    "page": "页码",
    "action": "操作",
    "resource": "资源类型",
    "rows": "记录数",
    "risk_level": "风险等级",
    "status": "个案状态",
    "actor": "操作人工号",
    "sensitive_raw_text": "包含学生敏感原文",
}

AUDIT_VALUE_LABELS = {
    "*": "全部",
    "included": "是",
    "excluded": "否",
    "CHAT": "普通交流",
    "CONSULT": "需要关注",
    "RISK": "高风险",
    "pending": "待处理",
    "notified": "已通知",
    "assigned": "已分配",
    "following": "跟进中",
    "closed": "已关闭",
    "student": "学生",
    "admin": "管理员",
    "active": "有效",
    "inactive": "停用",
    "disabled": "已停用",
}


def _audit_action_label(value: str) -> str:
    return AUDIT_ACTION_LABELS.get(value, value)


def _audit_resource_label(value: str) -> str:
    return AUDIT_RESOURCE_LABELS.get(value, value)


def _audit_value_label(value: str) -> str:
    return AUDIT_VALUE_LABELS.get(
        value,
        AUDIT_ACTION_LABELS.get(value, AUDIT_RESOURCE_LABELS.get(value, value)),
    )


def _audit_detail_label(value: str) -> str:
    if not value:
        return ""
    translated = []
    for segment in value.split(";"):
        segment = segment.strip()
        if not segment:
            continue
        if "=" in segment:
            key, raw_value = segment.split("=", 1)
            translated.append(
                f"{AUDIT_DETAIL_KEY_LABELS.get(key, key)}：{_audit_value_label(raw_value)}"
            )
        elif "->" in segment:
            previous, current = segment.split("->", 1)
            translated.append(
                f"状态：{_audit_value_label(previous)} → {_audit_value_label(current)}"
            )
        else:
            translated.append(_audit_value_label(segment))
    return "；".join(translated)


def _risk_decisions_from_trace(trace: AgentRunTrace, run: AgentRun) -> list[dict]:
    """Return the stage-by-stage risk decisions stored in the audit trace."""
    artifacts = json.loads(trace.artifacts_json)
    events = json.loads(trace.collaboration_events_json)
    degraded_by_actor = {
        event.get("actor"): bool(event.get("payload", {}).get("degraded"))
        for event in events
        if event.get("type") in {"INTENT_PUBLISHED", "RISK_PUBLISHED"}
    }
    stages = []
    for kind, stage, role in (
        ("intent", "understanding", "UnderstandingAgent 初步判断"),
        ("hard_rule", "hard_rule", "固定安全规则下限"),
        ("risk", "safety", "SafetyAgent 最终评估"),
    ):
        artifact = next(
            (item for item in reversed(artifacts) if item.get("kind") == kind),
            None,
        )
        if not artifact:
            continue
        payload = artifact.get("payload") or {}
        producer = artifact.get("producer") or ""
        stages.append(
            {
                "stage": stage,
                "role": role,
                "producer": producer,
                "risk_level": payload.get("risk_level"),
                "confidence": payload.get("confidence"),
                "reason": payload.get("reason") or payload.get("reason_summary"),
                "topics": payload.get("topics") or [],
                "needs_knowledge": payload.get("needs_knowledge"),
                "hard_rule_hit": payload.get("hard_rule_hit"),
                "immediate_danger": payload.get("immediate_danger"),
                "needs_safety_question": payload.get("needs_safety_question"),
                "degraded": degraded_by_actor.get(producer, False),
                "artifact_id": artifact.get("artifact_id"),
                "version": artifact.get("version"),
            }
        )
    stages.append(
        {
            "stage": "run_final",
            "role": "运行最终采纳结果",
            "producer": "CoordinatorAgent",
            "risk_level": run.risk_level,
            "confidence": None,
            "reason": None,
            "topics": [],
            "needs_knowledge": None,
            "hard_rule_hit": None,
            "immediate_danger": None,
            "needs_safety_question": None,
            "degraded": run.degraded,
            "artifact_id": None,
            "version": None,
        }
    )
    return stages


@router.get("/skills")
def list_skills(
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    del admin, db
    registry = SkillRegistry(request.app.state.skill_registry.skills_dir)
    return [item.to_public_dict() for item in registry.scan()]


@router.get("/knowledge/status")
def knowledge_status(
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
):
    del admin
    return request.app.state.rag_service.status()


@router.get("/knowledge/sources")
def knowledge_sources(
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
):
    del admin
    return request.app.state.rag_service.status()["sources"]


@router.post("/knowledge/index/rebuild")
def rebuild_knowledge_index(
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
):
    del admin
    return request.app.state.rag_service.rebuild()


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


@router.get("/users")
def list_users(
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    role: str | None = None,
):
    statement = select(User).order_by(User.id.desc())
    if role:
        if role not in {"student", "admin"}:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="用户角色必须是 student 或 admin。",
            )
        statement = statement.where(User.role == role)
    users = list(db.scalars(statement))
    del admin
    return [
        {
            "id": user.public_id,
            "institutional_id": user.institutional_id,
            "name": user.name,
            "role": user.role,
            "status": user.status,
            "created_at": user.created_at,
        }
        for user in users
    ]


@router.post("/users/admins", status_code=status.HTTP_201_CREATED)
def create_admin_user(
    payload: AccountRegistrationRequest,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    institutional_id = payload.institutional_id.strip()
    user = User(
        institutional_id=institutional_id,
        name=payload.name.strip(),
        password_hash=hash_password(payload.password),
        role="admin",
    )
    db.add(user)
    try:
        db.flush()
        _audit(
            db,
            admin,
            "admin_account_created",
            "user",
            user.public_id,
            detail=f"institutional_id={institutional_id}",
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="该工号已注册。",
        ) from None
    return {
        "id": user.public_id,
        "institutional_id": user.institutional_id,
        "name": user.name,
        "role": user.role,
        "status": user.status,
        "created_at": user.created_at,
    }


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
            .where(PsychologicalReport.risk_level == "RISK")
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
            "student_id": user.institutional_id,
            "student_name": user.name,
            "student": f"{user.name}（{user.institutional_id}）",
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
        "handoff_summary": report.handoff_summary,
        "notice": "仅用于必要的人工跟进；本报告由智能系统生成，不构成临床诊断。",
    }


@router.get("/cases")
def list_cases(
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    cases = list(
        db.scalars(
            select(RiskCase)
            .options(selectinload(RiskCase.owner))
            .order_by(RiskCase.id.desc())
        )
    )
    del admin
    return [
        {
            "id": item.public_id,
            "risk_level": item.risk_level,
            "status": item.status,
            "handoff_summary": item.handoff_summary,
            "note": item.note,
            "owner": (
                {
                    "id": item.owner.public_id,
                    "institutional_id": item.owner.institutional_id,
                    "name": item.owner.name,
                }
                if item.owner
                else None
            ),
            "created_at": item.created_at,
            "updated_at": item.updated_at,
        }
        for item in cases
    ]


def _case_or_404(db: Session, case_id: str) -> RiskCase:
    item = db.scalar(select(RiskCase).where(RiskCase.public_id == case_id))
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="个案不存在。")
    return item


@router.post("/cases/{case_id}/assign")
def assign_case_owner(
    case_id: str,
    payload: CaseAssignmentRequest,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    item = _case_or_404(db, case_id)
    if item.status == "closed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="已关闭个案不能分配负责人，请先重新打开。",
        )
    owner = db.scalar(
        select(User).where(
            User.public_id == payload.owner_id,
            User.role == "admin",
            User.status == "active",
        )
    )
    if not owner:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="负责人不存在或账号不可用。",
        )
    old_owner = db.get(User, item.owner_id) if item.owner_id else None
    if item.owner_id == owner.id:
        return {
            "id": item.public_id,
            "status": item.status,
            "owner": {
                "id": owner.public_id,
                "institutional_id": owner.institutional_id,
                "name": owner.name,
            },
        }
    old_status = item.status
    item.owner_id = owner.id
    if item.status in {"pending", "notified"}:
        item.status = "assigned"
    db.add(
        CaseEvent(
            case_id=item.id,
            event_type="owner_assigned",
            from_status=old_status,
            to_status=item.status,
            content=(
                f"{old_owner.name if old_owner else '未分配'} → "
                f"{owner.name}（{owner.institutional_id}）"
            ),
            actor_id=admin.id,
        )
    )
    _audit(
        db,
        admin,
        "case_owner_assigned",
        "case",
        item.public_id,
        detail=f"owner={owner.institutional_id}",
    )
    db.commit()
    return {
        "id": item.public_id,
        "status": item.status,
        "owner": {
            "id": owner.public_id,
            "institutional_id": owner.institutional_id,
            "name": owner.name,
        },
    }


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
        db.execute(
            select(CaseEvent, User)
            .outerjoin(User, User.id == CaseEvent.actor_id)
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
            "actor": (
                {
                    "institutional_id": actor.institutional_id,
                    "name": actor.name,
                }
                if actor
                else None
            ),
            "created_at": event.created_at,
        }
        for event, actor in events
    ]


@router.get("/audit-logs")
def list_audit_logs(
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    action: str | None = None,
    resource_type: str | None = None,
    actor: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    statement = select(AuditLog, User).join(User, User.id == AuditLog.actor_id)
    count_statement = select(func.count(AuditLog.id)).select_from(AuditLog).join(
        User, User.id == AuditLog.actor_id
    )
    if action:
        statement = statement.where(AuditLog.action == action)
        count_statement = count_statement.where(AuditLog.action == action)
    if resource_type:
        statement = statement.where(AuditLog.resource_type == resource_type)
        count_statement = count_statement.where(
            AuditLog.resource_type == resource_type
        )
    if actor:
        statement = statement.where(User.institutional_id.contains(actor.strip()))
        count_statement = count_statement.where(
            User.institutional_id.contains(actor.strip())
        )
    total = db.scalar(count_statement) or 0
    rows = db.execute(
        statement.order_by(AuditLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    del admin
    return {
        "items": [
            {
                "id": audit.id,
                "actor": {
                    "institutional_id": user.institutional_id,
                    "name": user.name,
                    "role": user.role,
                },
                "action": audit.action,
                "action_label": _audit_action_label(audit.action),
                "resource_type": audit.resource_type,
                "resource_type_label": _audit_resource_label(audit.resource_type),
                "resource_id": audit.resource_public_id,
                "detail": audit.detail,
                "detail_label": _audit_detail_label(audit.detail),
                "created_at": audit.created_at,
            }
            for audit, user in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/exports/{export_type}.xlsx")
def export_excel(
    export_type: str,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    risk_level: str | None = None,
    case_status: str | None = None,
    action: str | None = None,
    resource_type: str | None = None,
    actor: str | None = None,
):
    if export_type == "reports":
        statement = (
            select(PsychologicalReport, User, Message)
            .join(User, User.id == PsychologicalReport.student_id)
            .join(Message, Message.id == PsychologicalReport.source_message_id)
            .order_by(PsychologicalReport.id.desc())
        )
        if risk_level:
            statement = statement.where(PsychologicalReport.risk_level == risk_level)
        data = db.execute(statement).all()
        headers = (
            "报告ID",
            "学号",
            "学生姓名",
            "学生原始表达",
            "风险等级",
            "情绪摘要",
            "判断依据",
            "置信度",
            "AI生成",
            "生成时间",
        )
        rows = [
            (
                report.public_id,
                student.institutional_id,
                student.name,
                source_message.content,
                _audit_value_label(report.risk_level),
                report.emotion_summary,
                report.reason_summary,
                report.confidence,
                "是" if report.generated_by_ai else "否",
                report.created_at,
            )
            for report, student, source_message in data
        ]
        title, sheet_name = "MindNook 心理支持报告", "心理支持报告"
        filter_detail = f"risk_level={risk_level or '*'}"
        notice = "本文件完整包含学生原始表达等敏感原文，请妥善保管并控制访问范围。"
    elif export_type == "cases":
        statement = (
            select(RiskCase, User, PsychologicalReport, Message)
            .join(User, User.id == RiskCase.student_id)
            .join(PsychologicalReport, PsychologicalReport.id == RiskCase.report_id)
            .join(Message, Message.id == PsychologicalReport.source_message_id)
            .options(selectinload(RiskCase.owner))
            .order_by(RiskCase.id.desc())
        )
        if case_status:
            statement = statement.where(RiskCase.status == case_status)
        data = db.execute(statement).all()
        headers = (
            "个案ID",
            "学号",
            "学生姓名",
            "学生原始表达",
            "风险等级",
            "状态",
            "负责人工号",
            "负责人姓名",
            "交接摘要",
            "最近备注",
            "创建时间",
            "更新时间",
        )
        rows = [
            (
                item.public_id,
                student.institutional_id,
                student.name,
                source_message.content,
                _audit_value_label(item.risk_level),
                _audit_value_label(item.status),
                item.owner.institutional_id if item.owner else "",
                item.owner.name if item.owner else "未分配",
                item.handoff_summary,
                item.note or "",
                item.created_at,
                item.updated_at,
            )
            for item, student, _report, source_message in data
        ]
        title, sheet_name = "MindNook 风险个案", "风险个案"
        filter_detail = f"status={case_status or '*'}"
        notice = "本文件完整包含学生原始表达等敏感原文，请妥善保管并控制访问范围。"
    elif export_type == "audit-logs":
        statement = select(AuditLog, User).join(User, User.id == AuditLog.actor_id)
        if action:
            statement = statement.where(AuditLog.action == action)
        if resource_type:
            statement = statement.where(AuditLog.resource_type == resource_type)
        if actor:
            statement = statement.where(User.institutional_id.contains(actor.strip()))
        data = db.execute(statement.order_by(AuditLog.id.desc())).all()
        headers = (
            "审计ID",
            "操作人工号/学号",
            "操作人姓名",
            "角色",
            "操作",
            "资源类型",
            "资源ID",
            "详情",
            "操作时间",
        )
        rows = [
            (
                audit.id,
                user.institutional_id,
                user.name,
                _audit_value_label(user.role),
                _audit_action_label(audit.action),
                _audit_resource_label(audit.resource_type),
                audit.resource_public_id or "",
                _audit_detail_label(audit.detail),
                audit.created_at,
            )
            for audit, user in data
        ]
        title, sheet_name = "MindNook 审计日志", "审计日志"
        filter_detail = (
            f"action={action or '*'};resource={resource_type or '*'};actor={actor or '*'}"
        )
        notice = "本文件包含审计操作明细，请按审计数据规范保管。"
    else:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="不支持的导出类型。",
        )

    content = build_excel_export(
        title=title,
        sheet_name=sheet_name,
        headers=headers,
        rows=rows,
        notice=notice,
    )
    _audit(
        db,
        admin,
        "excel_exported",
        export_type,
        detail=f"rows={len(rows)};{filter_detail};sensitive_raw_text=included",
    )
    db.commit()
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    filename = f"mindnook_{export_type}_{timestamp}.xlsx"
    return Response(
        content=content,
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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
            "risk_level": item.risk_level,
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
        _audit(db, admin, "agent_audit_memory_viewed", "agent_run", run.public_id)
        db.commit()
        return {
            "run_id": run.public_id,
            "trace_available": False,
            "trace_notice": "该历史运行未保存阶段审计轨迹，无法恢复 UnderstandingAgent 当时的原始判断；当前仅保留运行最终风险等级。",
            "source_input": None,
            "redacted_input": None,
            "memoryBrief": "",
            "modelHistory": [],
            "agent_steps": [],
            "retrieval_results": [],
            "assessment": {},
            "risk_decisions": [],
            "collaboration_events": [],
            "tasks": [],
            "artifacts": [],
            "messages": [],
            "created_at": run.created_at,
        }
    _audit(db, admin, "agent_audit_memory_viewed", "agent_run", run.public_id)
    db.commit()
    return {
        "run_id": run.public_id,
        "trace_available": True,
        "trace_notice": None,
        "source_input": trace.source_input,
        "redacted_input": trace.redacted_input,
        "memoryBrief": trace.memory_brief,
        "modelHistory": json.loads(trace.model_history_json),
        "agent_steps": json.loads(trace.agent_steps_json),
        "retrieval_results": json.loads(trace.retrieval_results_json),
        "assessment": json.loads(trace.assessment_json),
        "risk_decisions": _risk_decisions_from_trace(trace, run),
        "collaboration_events": json.loads(trace.collaboration_events_json),
        "tasks": json.loads(trace.tasks_json),
        "artifacts": json.loads(trace.artifacts_json),
        "messages": json.loads(trace.messages_json),
        "created_at": trace.created_at,
    }
