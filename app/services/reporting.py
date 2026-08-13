from __future__ import annotations

from sqlalchemy import select

from app.models import Message, PsychologicalReport, RiskCase, User
from app.skills.registry import SkillRegistry


def backfill_handoff_summaries(session_factory, registry: SkillRegistry) -> int:
    """Fill missing report summaries and keep existing risk cases aligned."""
    db = session_factory()
    updated = 0
    try:
        reports = list(
            db.scalars(
                select(PsychologicalReport).where(
                    (PsychologicalReport.handoff_summary.is_(None))
                    | (PsychologicalReport.handoff_summary == "")
                )
            )
        )
        for report in reports:
            student = db.get(User, report.student_id)
            source = db.get(Message, report.source_message_id)
            report.handoff_summary = registry.render_handoff_summary(
                report_id=report.public_id,
                student_identity=(
                    f"{student.username}（{student.public_id}）" if student else "未知学生"
                ),
                risk_level=report.risk_level,
                emotion_summary=report.emotion_summary,
                confidence=report.confidence,
                reason_summary=report.reason_summary,
                student_expression=source.content if source else "",
            )
            risk_case = db.scalar(
                select(RiskCase).where(RiskCase.report_id == report.id)
            )
            if risk_case:
                risk_case.handoff_summary = report.handoff_summary
            updated += 1
        db.commit()
        return updated
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
