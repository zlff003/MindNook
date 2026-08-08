from __future__ import annotations

import os
import smtplib
import tempfile
import threading
from email.message import EmailMessage
from pathlib import Path

from openpyxl import Workbook, load_workbook
from sqlalchemy import select

from app.core.config import Settings
from app.models import (
    AlertDelivery,
    AlertEvent,
    AuditLog,
    CaseEvent,
    LedgerEntry,
    PsychologicalReport,
    RiskCase,
    User,
    utc_now,
)


LEDGER_HEADERS = (
    "report_id",
    "student_id",
    "created_at",
    "dialogue_type",
    "risk_level",
    "confidence",
    "emotion_summary",
    "reason_summary",
)
_EXCEL_WRITE_LOCK = threading.Lock()


class ToolOperations:
    """Shared idempotent business operations used by workers, APIs, and MCP."""

    def __init__(self, session_factory, settings: Settings):
        self.session_factory = session_factory
        self.settings = settings

    @staticmethod
    def _report(db, report_public_id: str) -> PsychologicalReport:
        report = db.scalar(
            select(PsychologicalReport).where(
                PsychologicalReport.public_id == report_public_id
            )
        )
        if not report:
            raise LookupError(f"report not found: {report_public_id}")
        return report

    @staticmethod
    def _case(db, case_public_id: str) -> RiskCase:
        risk_case = db.scalar(
            select(RiskCase).where(RiskCase.public_id == case_public_id)
        )
        if not risk_case:
            raise LookupError(f"case not found: {case_public_id}")
        return risk_case

    @staticmethod
    def _alert(db, alert_id: int) -> AlertDelivery:
        alert = db.get(AlertDelivery, alert_id)
        if not alert:
            raise LookupError(f"alert not found: {alert_id}")
        return alert

    def _write_excel(self, db, report: PsychologicalReport) -> None:
        path = Path(self.settings.excel_ledger_path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        student = db.get(User, report.student_id)
        student_public_id = student.public_id if student else "unknown"

        with _EXCEL_WRITE_LOCK:
            if path.exists():
                workbook = load_workbook(path)
                sheet = workbook.active
                current_headers = tuple(cell.value for cell in sheet[1])
                if current_headers != LEDGER_HEADERS:
                    workbook.close()
                    raise RuntimeError("Excel ledger header version is incompatible")
            else:
                workbook = Workbook()
                sheet = workbook.active
                sheet.title = "心理风险台账"
                sheet.append(LEDGER_HEADERS)
                sheet.freeze_panes = "A2"

            existing_report_ids = {
                str(cell.value) for cell in sheet["A"][1:] if cell.value is not None
            }
            if report.public_id not in existing_report_ids:
                sheet.append(
                    (
                        report.public_id,
                        student_public_id,
                        report.created_at.isoformat(),
                        report.dialogue_type,
                        report.risk_level,
                        report.confidence,
                        report.emotion_summary,
                        report.reason_summary,
                    )
                )

            temporary_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    prefix="mindnook-ledger-",
                    suffix=".xlsx",
                    dir=path.parent,
                    delete=False,
                ) as temporary_file:
                    temporary_path = Path(temporary_file.name)
                workbook.save(temporary_path)
                workbook.close()
                os.replace(temporary_path, path)
            finally:
                workbook.close()
                if temporary_path and temporary_path.exists():
                    temporary_path.unlink()

    def excel_report(self, report_public_id: str) -> dict:
        db = self.session_factory()
        try:
            report = self._report(db, report_public_id)
            existing = db.scalar(
                select(LedgerEntry).where(LedgerEntry.report_id == report.id)
            )
            if not existing:
                db.add(LedgerEntry(report_id=report.id))
            self._write_excel(db, report)
            db.commit()
            return {
                "ok": True,
                "report_id": report.public_id,
                "ledger_path": str(Path(self.settings.excel_ledger_path).resolve()),
                "idempotent": existing is not None,
            }
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def case_create(self, report_public_id: str) -> dict:
        db = self.session_factory()
        try:
            report = self._report(db, report_public_id)
            existing = db.scalar(
                select(RiskCase).where(RiskCase.report_id == report.id)
            )
            if existing:
                return {
                    "ok": True,
                    "report_id": report.public_id,
                    "case_id": existing.public_id,
                    "idempotent": True,
                }
            risk_case = RiskCase(
                report_id=report.id,
                student_id=report.student_id,
                risk_level=report.risk_level,
                status="pending",
                handoff_summary=(
                    f"{report.emotion_summary} {report.reason_summary} "
                    "建议管理员确认学生当前安全、是否有人陪伴，并按校内流程跟进。"
                ),
            )
            db.add(risk_case)
            db.flush()
            db.add(
                CaseEvent(
                    case_id=risk_case.id,
                    event_type="created",
                    to_status="pending",
                    content="系统根据中高风险报告自动创建个案。",
                )
            )
            db.commit()
            return {
                "ok": True,
                "report_id": report.public_id,
                "case_id": risk_case.public_id,
                "idempotent": False,
            }
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _email_enabled(self) -> bool:
        return bool(
            self.settings.smtp_host
            and self.settings.smtp_from_address
            and self.settings.alert_email_to
        )

    def _send_email(self, report: PsychologicalReport, risk_case: RiskCase) -> str:
        if not self._email_enabled():
            return "log"
        message = EmailMessage()
        message["Subject"] = f"[MindNook 高风险预警] 个案 {risk_case.public_id}"
        message["From"] = self.settings.smtp_from_address
        message["To"] = self.settings.alert_email_to
        message["Message-ID"] = f"<mindnook-{report.public_id}@mindbridge.local>"
        message.set_content(
            "MindNook 检测到需要人工复核的高风险报告。\n\n"
            f"个案 ID：{risk_case.public_id}\n"
            f"风险等级：{report.risk_level}\n"
            f"摘要：{report.emotion_summary}\n"
            "请登录管理后台查看，并按学校正式流程跟进。"
        )
        with smtplib.SMTP(
            self.settings.smtp_host,
            self.settings.smtp_port,
            timeout=10,
        ) as client:
            if self.settings.smtp_use_tls:
                client.starttls()
            if self.settings.smtp_username:
                client.login(
                    self.settings.smtp_username,
                    self.settings.smtp_password,
                )
            client.send_message(message)
        return "email"

    def alert_send(self, report_public_id: str) -> dict:
        db = self.session_factory()
        try:
            report = self._report(db, report_public_id)
            risk_case = db.scalar(
                select(RiskCase).where(RiskCase.report_id == report.id)
            )
            if not risk_case:
                raise RuntimeError("risk case must exist before alert")
            existing = db.scalar(
                select(AlertDelivery).where(AlertDelivery.report_id == report.id)
            )
            if existing and existing.status in {"sent", "acknowledged"}:
                return {
                    "ok": True,
                    "report_id": report.public_id,
                    "alert_id": existing.id,
                    "channel": existing.channel,
                    "idempotent": True,
                }
            alert = existing or AlertDelivery(
                report_id=report.id,
                channel="email" if self._email_enabled() else "log",
                status="pending",
                summary=f"高风险个案 {risk_case.public_id} 已创建，请管理员尽快查看。",
            )
            if not existing:
                db.add(alert)
                db.flush()
            channel = self._send_email(report, risk_case)
            alert.channel = channel
            alert.status = "sent"
            db.add(
                AlertEvent(
                    alert_id=alert.id,
                    event_type="sent",
                    content=f"预警已通过 {channel} 渠道处理。",
                )
            )
            db.commit()
            return {
                "ok": True,
                "report_id": report.public_id,
                "alert_id": alert.id,
                "channel": channel,
                "idempotent": False,
            }
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def alert_ack(
        self,
        alert_id: int,
        *,
        actor_username: str,
        note: str = "",
    ) -> dict:
        db = self.session_factory()
        try:
            alert = self._alert(db, alert_id)
            already = alert.status == "acknowledged"
            if already:
                return {"ok": True, "alert_id": alert.id, "idempotent": True}
            alert.status = "acknowledged"
            db.add(
                AlertEvent(
                    alert_id=alert.id,
                    event_type="acknowledged",
                    actor_username=actor_username.strip() or "mcp",
                    content=note.strip() or None,
                )
            )
            db.commit()
            return {"ok": True, "alert_id": alert.id, "idempotent": already}
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def case_note_add(
        self,
        case_public_id: str,
        note: str,
        *,
        actor_username: str = "mcp",
    ) -> dict:
        cleaned = note.strip()
        if not cleaned:
            raise ValueError("note must not be empty")
        db = self.session_factory()
        try:
            risk_case = self._case(db, case_public_id)
            actor = db.scalar(
                select(User).where(User.username == actor_username)
            )
            risk_case.note = (
                f"{risk_case.note}\n{cleaned}" if risk_case.note else cleaned
            )
            db.add(
                CaseEvent(
                    case_id=risk_case.id,
                    event_type="note_added",
                    content=cleaned,
                    actor_id=actor.id if actor else None,
                )
            )
            if actor:
                db.add(
                    AuditLog(
                        actor_id=actor.id,
                        action="case_note_added",
                        resource_type="case",
                        resource_public_id=risk_case.public_id,
                    )
                )
            db.commit()
            return {
                "ok": True,
                "case_id": risk_case.public_id,
                "note": risk_case.note,
                "added_note": cleaned,
                "status": risk_case.status,
            }
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def alert_notify(self, alert_id: int) -> dict:
        db = self.session_factory()
        try:
            alert = self._alert(db, alert_id)
            report = db.get(PsychologicalReport, alert.report_id)
            if not report:
                raise LookupError("alert report not found")
            risk_case = db.scalar(
                select(RiskCase).where(RiskCase.report_id == report.id)
            )
            if not risk_case:
                raise RuntimeError("risk case must exist before notification")
            channel = self._send_email(report, risk_case)
            alert.channel = channel
            alert.status = "sent"
            db.add(
                AlertEvent(
                    alert_id=alert.id,
                    event_type="notified",
                    actor_username="mcp",
                    content=f"预警已通过 {channel} 渠道再次通知。",
                    created_at=utc_now(),
                )
            )
            db.commit()
            return {"ok": True, "alert_id": alert.id, "channel": channel}
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
