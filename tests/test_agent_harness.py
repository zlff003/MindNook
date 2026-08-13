import tempfile
import unittest
from pathlib import Path

from sqlalchemy import func, select

from app.agents.runtime import AgentRuntime
from app.core.config import Settings
from app.db import Base, build_engine, build_session_factory
from app.harness.agent_harness import AgentHarness, ChatCommand
from app.models import (
    AgentRun,
    AgentRunTrace,
    AgentCollaborationEvent,
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
from app.services.chat import ChatService
from app.services.model import MockModelProvider
from app.services.tool_dispatcher import InlineToolDispatcher


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


class AgentHarnessTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(self.temp_dir.name) / "harness-test.db"
        self.engine = build_engine(f"sqlite:///{database_path.as_posix()}")
        Base.metadata.create_all(self.engine)
        self.session_factory = build_session_factory(self.engine)

        db = self.session_factory()
        try:
            student = User(
                username="harness-student",
                display_name="Harness Student",
                password_hash="unused-in-harness-test",
                role="student",
                status="active",
            )
            db.add(student)
            db.flush()
            conversation = Conversation(student_id=student.id)
            db.add(conversation)
            db.commit()
            self.student_public_id = student.public_id
            self.conversation_public_id = conversation.public_id
        finally:
            db.close()

        self.provider = RecordingModelProvider()
        self.settings = Settings(
            app_env="test",
            model_provider="mock",
            model_name="test-model",
            model_max_calls_per_run=7,
            model_run_timeout_seconds=10,
            excel_ledger_path=str(Path(self.temp_dir.name) / "risk-ledger.xlsx"),
        )
        self.runtime = AgentRuntime(self.provider, self.settings)
        self.dispatcher = InlineToolDispatcher(
            self.session_factory,
            self.settings.excel_ledger_path,
        )
        self.harness = AgentHarness(
            self.session_factory,
            self.runtime,
            self.dispatcher,
        )

    def tearDown(self):
        self.engine.dispose()
        self.temp_dir.cleanup()

    def _count(self, model) -> int:
        db = self.session_factory()
        try:
            return db.scalar(select(func.count()).select_from(model)) or 0
        finally:
            db.close()

    def _command(self, *, request_id: str = "harness-request-0001") -> ChatCommand:
        return ChatCommand(
            student_public_id=self.student_public_id,
            conversation_public_id=self.conversation_public_id,
            content="我的电话是 13800138000，我想自杀，现在完全撑不住了",
            client_request_id=request_id,
        )

    def test_full_business_chain_runs_without_http(self):
        events = list(self.harness.run(self._command()))
        event_types = [event.type for event in events]

        self.assertIn("accepted", event_types)
        self.assertIn("runtime_event", event_types)
        self.assertIn("runtime_plan", event_types)
        self.assertIn("model_progress", event_types)
        self.assertIn("delta", event_types)
        self.assertIn("completed", event_types)
        self.assertIn("trace", event_types)
        self.assertFalse(next(event for event in events if event.type == "runtime_plan").public)
        self.assertFalse(next(event for event in events if event.type == "trace").public)

        model_input = "\n".join(self.provider.seen_contents)
        self.assertNotIn("13800138000", model_input)
        self.assertIn("[PHONE]", model_input)

        self.assertEqual(self._count(Message), 2)
        self.assertEqual(self._count(AgentRun), 1)
        self.assertEqual(self._count(AgentRunTrace), 1)
        self.assertGreaterEqual(self._count(AgentStep), 7)
        self.assertGreater(self._count(AgentCollaborationEvent), 20)
        self.assertEqual(self._count(PsychologicalReport), 1)
        self.assertEqual(self._count(BackgroundJob), 3)
        self.assertEqual(self._count(LedgerEntry), 1)
        self.assertEqual(self._count(RiskCase), 1)
        self.assertEqual(self._count(AlertDelivery), 1)
        self.assertTrue(Path(self.settings.excel_ledger_path).is_file())

        trace = next(event.payload for event in events if event.type == "trace")
        self.assertEqual(
            trace["tool_plan"],
            ["ledger_write", "case_create", "alert_send"],
        )

    def test_chat_service_exposes_only_public_events_and_is_idempotent(self):
        command = self._command(request_id="harness-idempotency-0001")
        first_events = list(ChatService(self.harness).stream(command))
        second_events = list(ChatService(self.harness).stream(command))

        self.assertTrue(first_events)
        self.assertTrue(all(event.public for event in first_events))
        self.assertNotIn("runtime_plan", [event.type for event in first_events])
        self.assertNotIn("model_progress", [event.type for event in first_events])
        self.assertNotIn("trace", [event.type for event in first_events])
        self.assertTrue(second_events[0].payload["duplicate"])
        self.assertEqual(self._count(Message), 2)
        self.assertEqual(self._count(AgentRun), 1)
        self.assertEqual(self._count(PsychologicalReport), 1)

    def test_runtime_plan_has_inference_outputs_but_no_delivery_side_effects(self):
        plan = self.runtime.plan("最近考试压力很大，晚上睡不着", [])
        step_names = [step.step_name for step in plan.board.trace.steps]

        self.assertEqual(plan.intent.risk_level, "CONSULT")
        self.assertEqual(plan.risk.risk_level, "CONSULT")
        self.assertIsInstance(plan.knowledge, list)
        self.assertEqual(len(plan.response_messages), 2)
        self.assertIn("response_prompt", step_names)
        self.assertGreater(len(plan.events), len(plan.steps))
        self.assertIn("TASK_CLAIMED", [event.type for event in plan.events])
        self.assertIn("ARTIFACT_PUBLISHED", [event.type for event in plan.events])
        self.assertNotIn("response_generation", step_names)
        self.assertEqual(self._count(Message), 0)
        self.assertEqual(self._count(PsychologicalReport), 0)
        self.assertEqual(self._count(BackgroundJob), 0)

    def test_missing_conversation_is_a_stable_harness_error(self):
        events = list(
            self.harness.run(
                ChatCommand(
                    student_public_id=self.student_public_id,
                    conversation_public_id="missing-conversation",
                    content="你好",
                    client_request_id="harness-missing-0001",
                )
            )
        )

        self.assertEqual([event.type for event in events], ["error"])
        self.assertEqual(events[0].payload["code"], "CHAT_CONTEXT_NOT_FOUND")
        self.assertEqual(self._count(Message), 0)

    def test_second_turn_reads_short_term_memory_from_redis(self):
        first = ChatCommand(
            student_public_id=self.student_public_id,
            conversation_public_id=self.conversation_public_id,
            content="请帮我解释 Python 循环",
            client_request_id="memory-first-0001",
        )
        second = ChatCommand(
            student_public_id=self.student_public_id,
            conversation_public_id=self.conversation_public_id,
            content="最近考试压力很大",
            client_request_id="memory-second-0001",
        )
        list(self.harness.run(first))
        events = list(self.harness.run(second))

        runtime_plan = next(
            event.payload for event in events if event.type == "runtime_plan"
        )
        self.assertTrue(runtime_plan["memory"]["cache_hit"])
        self.assertEqual(runtime_plan["memory"]["source"], "redis")
        self.assertEqual(runtime_plan["memory"]["model_history_count"], 2)
        model_inputs = "\n".join(self.provider.seen_contents)
        self.assertIn("请帮我解释 Python 循环", model_inputs)


if __name__ == "__main__":
    unittest.main()
