import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.agents.runtime import CoordinatorAgent
from app.core.config import Settings
from app.main import create_app
from app.services.model import MockModelProvider
from app.skills.registry import REQUIRED_SKILLS, SkillRegistry, SkillStatus


PROJECT_SKILLS = Path(__file__).resolve().parents[1] / "skills"


def write_skill(root: Path, directory: str, source: str) -> None:
    target = root / directory
    target.mkdir(parents=True, exist_ok=True)
    (target / "SKILL.md").write_text(source, encoding="utf-8")


class SkillRegistryTests(unittest.TestCase):
    def test_project_skills_are_ready(self):
        registry = SkillRegistry(PROJECT_SKILLS)
        skills = registry.scan()
        self.assertEqual({item.directory_name for item in skills}, set(REQUIRED_SKILLS))
        self.assertTrue(all(item.status == SkillStatus.READY for item in skills))

    def test_validation_exposes_warn_and_failed(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            write_skill(root, "valid_skill", "---\nname: valid_skill\ndescription:\nextra: value\n---\n# Valid\n\n## Workflow\n")
            write_skill(root, "wrong_name", "---\nname: another_name\ndescription: x\n---\n## Workflow\n1. x")
            write_skill(root, "missing_frontmatter", "# Skill\n\n## Workflow\n1. x")
            indexed = {item.directory_name: item for item in SkillRegistry(root).scan()}
            self.assertEqual(indexed["valid_skill"].status, SkillStatus.WARN)
            self.assertEqual(indexed["wrong_name"].status, SkillStatus.FAILED)
            self.assertEqual(indexed["missing_frontmatter"].status, SkillStatus.FAILED)
            self.assertEqual(indexed["supportive_response_baseline"].status, SkillStatus.FAILED)

    def test_deterministic_response_skill_routing(self):
        registry = SkillRegistry(PROJECT_SKILLS)
        registry.scan()
        self.assertEqual(registry.select_response_skills(risk_level="CHAT", message="Python", topics=[]), ())
        anxiety = {item.name for item in registry.select_response_skills(risk_level="CONSULT", message="我很焦虑，脑子停不下来", topics=[])}
        combined = {item.name for item in registry.select_response_skills(risk_level="CONSULT", message="论文让我失眠，明天还有考试", topics=[])}
        risk = {item.name for item in registry.select_response_skills(risk_level="RISK", message="我想伤害自己", topics=[])}
        self.assertIn("supportive_response_baseline", anxiety)
        self.assertIn("referral_resource_guidance", anxiety)
        self.assertIn("anxiety_grounding_support", anxiety)
        self.assertIn("sleep_routine_support", combined)
        self.assertIn("academic_stress_planning", combined)
        self.assertEqual(risk, {"referral_resource_guidance", "high_risk_safety_plan"})

    def test_context_agent_injects_workflow_into_response_prompt(self):
        settings = Settings(app_env="test", model_provider="mock", skills_dir=str(PROJECT_SKILLS))
        runtime = CoordinatorAgent(MockModelProvider(), settings)
        chat = runtime.plan("请解释 Python 列表推导式")
        consult = runtime.plan("考试压力很大，晚上睡不着")
        prompt = "\n".join(item.content for item in consult.response_messages)
        self.assertEqual(chat.context.skills, [])
        self.assertIn("supportive_response_baseline", consult.context.skills)
        self.assertIn("sleep_routine_support", consult.context.skills)
        self.assertIn("academic_stress_planning", consult.context.skills)
        self.assertIn("Skill Workflow", prompt)
        self.assertIn("不把成绩", prompt)


class SkillApiTests(unittest.TestCase):
    def test_admin_can_view_skill_status_and_report_handoff(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            settings = Settings(app_env="test", app_secret="skill-test-secret", database_url=f"sqlite:///{(Path(temporary_dir) / 'skills.db').as_posix()}", model_provider="mock", skills_dir=str(PROJECT_SKILLS))
            with TestClient(create_app(settings)) as client:
                student_token = client.post("/api/v1/auth/login", json={"username": "student", "password": "student123"}).json()["access_token"]
                admin_token = client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin123"}).json()["access_token"]
                student_headers = {"Authorization": f"Bearer {student_token}"}
                admin_headers = {"Authorization": f"Bearer {admin_token}"}
                conversation = client.post("/api/v1/conversations", headers=student_headers).json()["id"]
                client.post(f"/api/v1/conversations/{conversation}/messages:stream", headers=student_headers, json={"content": "最近考试压力很大，晚上睡不着", "client_request_id": "skill-handoff-0001"})
                skills = client.get("/api/v1/admin/skills", headers=admin_headers).json()
                reports = client.get("/api/v1/admin/reports", headers=admin_headers).json()
                context = client.get(f"/api/v1/admin/reports/{reports[0]['id']}/context", headers=admin_headers).json()
                self.assertEqual(len(skills), 7)
                self.assertTrue(all(item["status"] == "READY" for item in skills))
                self.assertNotIn("handoff_summary", reports[0])
                self.assertIn(f"报告ID：{reports[0]['id']}", context["handoff_summary"])
                self.assertIn("风险等级：CONSULT", context["handoff_summary"])
                self.assertIn("原始表达摘录", context["handoff_summary"])


if __name__ == "__main__":
    unittest.main()
