import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.agents.runtime import AgentRuntime
from app.core.config import Settings
from app.main import create_app
from app.models import (
    AlertDelivery,
    AgentCollaborationEvent,
    AgentRun,
    AgentRunTrace,
    AgentStep,
    AuditLog,
    CaseEvent,
    LedgerEntry,
    Message,
    PsychologicalReport,
    RiskCase,
)
from app.services.model import MockModelProvider


class RecordingModelProvider(MockModelProvider):
    def __init__(self):
        self.seen_contents: list[str] = []

    def complete(self, messages, *, model, temperature, json_mode, purpose):
        self.seen_contents.extend(message.content for message in messages)
        return super().complete(
            messages,
            model=model,
            temperature=temperature,
            json_mode=json_mode,
            purpose=purpose,
        )


class MindNookApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(self.temp_dir.name) / "mindnook-test.db"
        settings = Settings(
            app_env="test",
            app_secret="test-secret-that-is-not-used-in-production",
            database_url=f"sqlite:///{database_path.as_posix()}",
            model_provider="mock",
            demo_student_username="student",
            demo_student_password="student123",
            demo_admin_username="admin",
            demo_admin_password="admin123",
            excel_ledger_path=str(Path(self.temp_dir.name) / "risk-ledger.xlsx"),
        )
        self.client_context = TestClient(create_app(settings))
        self.client = self.client_context.__enter__()
        self.student_token = self._login("student", "student123")
        self.admin_token = self._login("admin", "admin123")

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        self.temp_dir.cleanup()

    def _login(self, username: str, password: str) -> str:
        response = self.client.post(
            "/api/v1/auth/login",
            json={"username": username, "password": password},
        )
        self.assertEqual(response.status_code, 200)
        return response.json()["access_token"]

    def _headers(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def _create_conversation(self) -> str:
        response = self.client.post(
            "/api/v1/conversations",
            headers=self._headers(self.student_token),
        )
        self.assertEqual(response.status_code, 200)
        return response.json()["id"]

    def _chat(self, conversation_id: str, content: str, request_id: str):
        return self.client.post(
            f"/api/v1/conversations/{conversation_id}/messages:stream",
            headers=self._headers(self.student_token),
            json={
                "content": content,
                "client_request_id": request_id,
            },
        )

    def _count(self, model) -> int:
        session = self.client.app.state.session_factory()
        try:
            return session.scalar(select(func.count()).select_from(model)) or 0
        finally:
            session.close()

    def test_pages_and_health_are_available(self):
        self.assertEqual(self.client.get("/health/live").status_code, 200)
        self.assertEqual(self.client.get("/student").status_code, 200)
        admin_page = self.client.get("/admin")
        self.assertEqual(admin_page.status_code, 200)
        for route in ("overview", "reports", "cases", "alerts", "operations"):
            self.assertIn(f'data-route="{route}"', admin_page.text)
            self.assertIn(f'data-page="{route}"', admin_page.text)
        self.assertIn('id="detailDrawer"', admin_page.text)
        self.assertIn('id="reportPagination"', admin_page.text)

    def test_only_two_roles_can_log_in_to_their_apis(self):
        student_admin_response = self.client.get(
            "/api/v1/admin/reports",
            headers=self._headers(self.student_token),
        )
        self.assertEqual(student_admin_response.status_code, 403)

        admin_student_response = self.client.post(
            "/api/v1/conversations",
            headers=self._headers(self.admin_token),
        )
        self.assertEqual(admin_student_response.status_code, 403)

    def test_general_chat_streams_without_report(self):
        conversation_id = self._create_conversation()
        response = self._chat(
            conversation_id,
            "请帮我解释 Python 循环",
            "request-general-0001",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("event: accepted", response.text)
        self.assertIn("event: delta", response.text)
        self.assertIn("event: completed", response.text)
        self.assertEqual(self._count(PsychologicalReport), 0)
        self.assertEqual(self._count(RiskCase), 0)
        self.assertEqual(self._count(AlertDelivery), 0)

    def test_low_risk_support_creates_report_and_ledger_only(self):
        conversation_id = self._create_conversation()
        response = self._chat(
            conversation_id,
            "最近考试压力很大，晚上睡不着",
            "request-support-0001",
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("风险等级", response.text)
        self.assertEqual(self._count(PsychologicalReport), 1)
        self.assertEqual(self._count(LedgerEntry), 1)
        self.assertEqual(self._count(RiskCase), 0)
        self.assertEqual(self._count(AlertDelivery), 0)

    def test_medium_risk_creates_case(self):
        conversation_id = self._create_conversation()
        response = self._chat(
            conversation_id,
            "我每天都很焦虑，已经无法学习",
            "request-medium-0001",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._count(PsychologicalReport), 1)
        self.assertEqual(self._count(RiskCase), 1)
        self.assertEqual(self._count(AlertDelivery), 0)

    def test_high_risk_creates_full_follow_up_chain(self):
        conversation_id = self._create_conversation()
        response = self._chat(
            conversation_id,
            "我想自杀，现在完全撑不住了",
            "request-high-0001",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("不要独处", response.text)
        self.assertIn("信任的人", response.text)
        self.assertNotIn("你的风险等级", response.text)
        self.assertEqual(self._count(PsychologicalReport), 1)
        self.assertEqual(self._count(LedgerEntry), 1)
        self.assertEqual(self._count(RiskCase), 1)
        self.assertEqual(self._count(AlertDelivery), 1)

    def test_duplicate_request_is_idempotent(self):
        conversation_id = self._create_conversation()
        first = self._chat(
            conversation_id,
            "最近考试压力很大",
            "request-duplicate-0001",
        )
        second = self._chat(
            conversation_id,
            "最近考试压力很大",
            "request-duplicate-0001",
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertIn('"duplicate": true', second.text)
        self.assertEqual(self._count(PsychologicalReport), 1)
        self.assertEqual(self._count(LedgerEntry), 1)
        self.assertEqual(self._count(Message), 2)

    def test_report_context_access_is_audited(self):
        conversation_id = self._create_conversation()
        self._chat(
            conversation_id,
            "最近考试压力很大",
            "request-audit-0001",
        )
        reports = self.client.get(
            "/api/v1/admin/reports",
            headers=self._headers(self.admin_token),
        ).json()
        response = self.client.get(
            f"/api/v1/admin/reports/{reports[0]['id']}/context",
            headers=self._headers(self.admin_token),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._count(AuditLog), 1)

    def test_agent_run_and_steps_are_persisted_and_visible_to_admin(self):
        conversation_id = self._create_conversation()
        self._chat(
            conversation_id,
            "最近考试压力很大，晚上睡不着",
            "request-agent-trace-0001",
        )

        self.assertEqual(self._count(AgentRun), 1)
        self.assertEqual(self._count(AgentRunTrace), 1)
        self.assertGreaterEqual(self._count(AgentStep), 6)
        self.assertGreater(self._count(AgentCollaborationEvent), 20)
        runs_response = self.client.get(
            "/api/v1/admin/agent-runs",
            headers=self._headers(self.admin_token),
        )
        self.assertEqual(runs_response.status_code, 200)
        run = runs_response.json()[0]
        self.assertEqual(run["route"], "psychological_support")
        self.assertEqual(run["provider"], "mock")
        self.assertFalse(run["degraded"])

        steps_response = self.client.get(
            f"/api/v1/admin/agent-runs/{run['id']}/steps",
            headers=self._headers(self.admin_token),
        )
        self.assertEqual(steps_response.status_code, 200)
        step_names = [item["step_name"] for item in steps_response.json()]
        self.assertIn("hard_safety_rules", step_names)
        self.assertIn("response_review", step_names)

        events_response = self.client.get(
            f"/api/v1/admin/agent-runs/{run['id']}/events",
            headers=self._headers(self.admin_token),
        )
        self.assertEqual(events_response.status_code, 200)
        event_types = [item["event_type"] for item in events_response.json()]
        self.assertIn("TASK_CLAIMED", event_types)
        self.assertIn("ARTIFACT_PUBLISHED", event_types)
        self.assertIn("FINAL_ADOPTED", event_types)

        trace_response = self.client.get(
            f"/api/v1/admin/agent-runs/{run['id']}/trace",
            headers=self._headers(self.admin_token),
        )
        self.assertEqual(trace_response.status_code, 200)
        audit_memory = trace_response.json()
        self.assertIn("memoryBrief", audit_memory)
        self.assertIn("modelHistory", audit_memory)
        self.assertTrue(audit_memory["agent_steps"])
        self.assertTrue(audit_memory["collaboration_events"])
        self.assertTrue(audit_memory["tasks"])
        self.assertTrue(audit_memory["artifacts"])

    def test_raw_pii_never_enters_agent_model_messages(self):
        provider = RecordingModelProvider()
        runtime = AgentRuntime(
            provider,
            self.client.app.state.settings,
        )
        self.client.app.state.agent_runtime = runtime
        self.client.app.state.agent_harness.runtime = runtime
        conversation_id = self._create_conversation()
        response = self._chat(
            conversation_id,
            "我的电话是13800138000，最近考试压力很大",
            "request-redaction-0001",
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("13800138000", "\n".join(provider.seen_contents))
        self.assertIn("[PHONE]", "\n".join(provider.seen_contents))

        run = self.client.get(
            "/api/v1/admin/agent-runs",
            headers=self._headers(self.admin_token),
        ).json()[0]
        trace = self.client.get(
            f"/api/v1/admin/agent-runs/{run['id']}/trace",
            headers=self._headers(self.admin_token),
        ).json()
        self.assertIn("13800138000", trace["source_input"])
        self.assertNotIn("13800138000", trace["redacted_input"])
        self.assertIn("[PHONE]", trace["redacted_input"])

    def test_case_notes_and_status_keep_append_only_history(self):
        conversation_id = self._create_conversation()
        self._chat(
            conversation_id,
            "我每天都很焦虑，已经无法学习",
            "request-case-history-0001",
        )
        cases = self.client.get(
            "/api/v1/admin/cases",
            headers=self._headers(self.admin_token),
        ).json()
        case_id = cases[0]["id"]

        note_response = self.client.post(
            f"/api/v1/admin/cases/{case_id}/notes",
            headers=self._headers(self.admin_token),
            json={"note": "已联系学生，安排进一步沟通。"},
        )
        transition_response = self.client.post(
            f"/api/v1/admin/cases/{case_id}/transitions",
            headers=self._headers(self.admin_token),
            json={"status": "following"},
        )
        history_response = self.client.get(
            f"/api/v1/admin/cases/{case_id}/events",
            headers=self._headers(self.admin_token),
        )

        self.assertEqual(note_response.status_code, 200)
        self.assertEqual(transition_response.status_code, 200)
        self.assertEqual(history_response.status_code, 200)
        event_types = [item["event_type"] for item in history_response.json()]
        self.assertEqual(event_types, ["created", "note_added", "status_changed"])
        self.assertEqual(self._count(CaseEvent), 3)


if __name__ == "__main__":
    unittest.main()
