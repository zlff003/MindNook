import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select

from app.agents.runtime import CoordinatorAgent
from app.core.config import Settings
from app.db import Base, build_engine, build_session_factory
from app.memory.private import AgentPrivateMemory
from app.memory.service import ContextCompressor, ShortTermMemoryService
from app.memory.store import InMemoryRedisStore
from app.memory.types import MemoryContext, MemoryMessage
from app.models import Conversation, Message, User
from app.services.model import MockModelProvider


def memory_settings(**overrides) -> Settings:
    values = {
        "app_env": "test",
        "model_provider": "mock",
        "model_name": "test-model",
        "short_term_memory_prefix": "mindbridge:short-term-memory",
        "short_term_memory_max_messages": 40,
        "short_term_memory_ttl_seconds": 86400,
        "memory_recent_messages": 8,
        "memory_brief_max_chars": 1200,
        "memory_model_history_max_chars": 6000,
        "agent_private_memory_max_entries": 20,
        "agent_private_memory_ttl_seconds": 86400,
    }
    values.update(overrides)
    return Settings(**values)


class UnavailableRedis:
    def lrange(self, *args):
        raise OSError("redis unavailable")

    def delete(self, *args):
        raise OSError("redis unavailable")

    def rpush(self, *args):
        raise OSError("redis unavailable")

    def ltrim(self, *args):
        raise OSError("redis unavailable")

    def expire(self, *args):
        raise OSError("redis unavailable")


class PurposeRecordingProvider(MockModelProvider):
    def __init__(self):
        self.by_purpose: dict[str, list[str]] = {}

    def complete(self, messages, *, model, temperature, json_mode, purpose):
        self.by_purpose.setdefault(purpose, []).extend(
            message.content for message in messages
        )
        return super().complete(
            messages,
            model=model,
            temperature=temperature,
            json_mode=json_mode,
            purpose=purpose,
        )


class LayeredMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(self.temp_dir.name) / "memory-test.db"
        self.engine = build_engine(f"sqlite:///{database_path.as_posix()}")
        Base.metadata.create_all(self.engine)
        self.session_factory = build_session_factory(self.engine)
        db = self.session_factory()
        try:
            student = User(
                username="memory-student",
                display_name="Memory Student",
                password_hash="unused",
                role="student",
                status="active",
            )
            db.add(student)
            db.flush()
            conversation = Conversation(student_id=student.id)
            db.add(conversation)
            db.commit()
            self.conversation_id = conversation.id
            self.session_public_id = conversation.public_id
        finally:
            db.close()

    def tearDown(self):
        self.engine.dispose()
        self.temp_dir.cleanup()

    def _seed_messages(self, count: int) -> None:
        db = self.session_factory()
        try:
            for index in range(count):
                role = "student" if index % 2 == 0 else "assistant"
                content = (
                    "今晚我又有点撑不住，想联系同学求助"
                    if index == 10
                    else f"第 {index:02d} 条历史消息"
                )
                db.add(
                    Message(
                        conversation_id=self.conversation_id,
                        sender_type=role,
                        content=content,
                        redacted_content=content,
                        status="completed",
                    )
                )
            db.commit()
        finally:
            db.close()

    def test_redis_miss_backfills_latest_40_then_next_read_hits_cache(self):
        self._seed_messages(45)
        settings = memory_settings()
        store = InMemoryRedisStore()
        service = ShortTermMemoryService(settings, store)
        db = self.session_factory()
        try:
            conversation = db.get(Conversation, self.conversation_id)
            first = service.load(db, conversation)
            second = service.load(db, conversation)
            database_count = db.scalar(select(func.count()).select_from(Message))
        finally:
            db.close()

        key = f"mindbridge:short-term-memory:{self.session_public_id}"
        self.assertEqual(database_count, 45)
        self.assertEqual(Message.__tablename__, "chat_messages")
        self.assertEqual(first.source, "mysql")
        self.assertFalse(first.cache_hit)
        self.assertEqual(first.cached_messages, 40)
        self.assertEqual(second.source, "redis")
        self.assertTrue(second.cache_hit)
        self.assertEqual(len(store.lrange(key, 0, -1)), 40)
        self.assertGreater(store.ttl(key), 86000)
        self.assertEqual(len(first.model_history), 8)
        self.assertEqual(first.model_history[-1].content, "第 44 条历史消息")
        self.assertIn("今晚我又有点撑不住", first.memory_brief)

    def test_model_history_keeps_latest_eight_with_role_and_char_budget(self):
        settings = memory_settings(memory_model_history_max_chars=280)
        compressor = ContextCompressor(settings)
        messages = [
            MemoryMessage(
                message_id=str(index),
                role="student" if index % 2 == 0 else "assistant",
                content=(f"消息 {index} " + "内容" * 40),
                created_at="",
            )
            for index in range(20)
        ]
        context = compressor.compress(
            self.session_public_id,
            messages,
            source="redis",
            cache_hit=True,
        )

        self.assertLessEqual(len(context.model_history), 8)
        self.assertEqual(context.model_history[-1].message_id, "19")
        self.assertIn(context.model_history[-1].role, {"student", "assistant"})
        self.assertLessEqual(
            sum(len(item.content) + 12 for item in context.model_history),
            280,
        )
        self.assertLessEqual(len(context.memory_brief), settings.memory_brief_max_chars)

    def test_append_refreshes_ttl_and_trims_to_40(self):
        settings = memory_settings()
        store = InMemoryRedisStore()
        service = ShortTermMemoryService(settings, store)
        for index in range(45):
            service.append(
                self.session_public_id,
                Message(
                    public_id=f"message-{index}",
                    conversation_id=self.conversation_id,
                    sender_type="student",
                    content=f"message {index}",
                    redacted_content=f"message {index}",
                    status="completed",
                    created_at=datetime.now(UTC),
                ),
            )

        key = service.key(self.session_public_id)
        values = store.lrange(key, 0, -1)
        self.assertEqual(len(values), 40)
        self.assertIn("message-44", values[-1])
        self.assertGreater(store.ttl(key), 86000)

    def test_redis_outage_falls_back_to_database(self):
        self._seed_messages(3)
        service = ShortTermMemoryService(memory_settings(), UnavailableRedis())
        db = self.session_factory()
        try:
            context = service.load(db, db.get(Conversation, self.conversation_id))
        finally:
            db.close()

        self.assertEqual(context.source, "mysql")
        self.assertFalse(context.cache_hit)
        self.assertEqual(len(context.model_history), 3)

    def test_agent_private_memory_uses_isolated_keys_and_limits_entries(self):
        settings = memory_settings(agent_private_memory_max_entries=2)
        store = InMemoryRedisStore()
        private = AgentPrivateMemory(settings, store)
        private.append("UnderstandingAgent", self.session_public_id, {"intent": "one"})
        private.append("UnderstandingAgent", self.session_public_id, {"intent": "two"})
        private.append("UnderstandingAgent", self.session_public_id, {"intent": "three"})
        private.append("SafetyAgent", self.session_public_id, {"risk": "high"})

        snapshot = private.load_for_agents(self.session_public_id)
        understanding_key = f"agent:UnderstandingAgent:{self.session_public_id}"
        safety_key = f"agent:SafetyAgent:{self.session_public_id}"
        self.assertEqual(len(snapshot["UnderstandingAgent"]), 2)
        self.assertEqual(len(snapshot["SafetyAgent"]), 1)
        self.assertEqual(snapshot["ResponseAgent"], ())
        self.assertNotEqual(understanding_key, safety_key)
        self.assertGreater(store.ttl(understanding_key), 86000)

    def test_runtime_only_injects_each_agents_own_private_memory(self):
        provider = PurposeRecordingProvider()
        runtime = CoordinatorAgent(provider, memory_settings())
        memory_context = MemoryContext(
            session_public_id=self.session_public_id,
            memory_brief="较早记忆摘要标记",
            model_history=(
                MemoryMessage("1", "student", "我上次提到考试", ""),
                MemoryMessage("2", "assistant", "我们讨论了拆分任务", ""),
            ),
            source="redis",
            cache_hit=True,
            total_messages=10,
            cached_messages=10,
        )
        private_memory = {
            "UnderstandingAgent": ("UNDERSTANDING_PRIVATE_MARKER",),
            "SafetyAgent": ("SAFETY_PRIVATE_MARKER",),
            "ResponseAgent": ("RESPONSE_PRIVATE_MARKER",),
        }
        plan = runtime.plan(
            "最近考试压力很大",
            memory_context=memory_context,
            private_memory=private_memory,
        )
        candidate = "".join(runtime.stream_response(plan))
        runtime.finalize_response(plan, candidate)

        understanding_prompt = "\n".join(provider.by_purpose["understanding"])
        risk_prompt = "\n".join(provider.by_purpose["risk_assessment"])
        response_prompt = "\n".join(provider.by_purpose["response_generation"])
        self.assertIn("UNDERSTANDING_PRIVATE_MARKER", understanding_prompt)
        self.assertNotIn("SAFETY_PRIVATE_MARKER", understanding_prompt)
        self.assertIn("SAFETY_PRIVATE_MARKER", risk_prompt)
        self.assertNotIn("RESPONSE_PRIVATE_MARKER", risk_prompt)
        self.assertIn("RESPONSE_PRIVATE_MARKER", response_prompt)
        self.assertNotIn("SAFETY_PRIVATE_MARKER", response_prompt)
        self.assertIn("较早记忆摘要标记", response_prompt)
        self.assertIn("我上次提到考试", response_prompt)


if __name__ == "__main__":
    unittest.main()
