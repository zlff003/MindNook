from __future__ import annotations

import json
import re
from dataclasses import replace

from redis.exceptions import RedisError
from sqlalchemy import select

from app.core.config import Settings
from app.memory.types import MemoryContext, MemoryMessage
from app.models import Conversation, Message
from app.services.privacy import redact_pii


IMPORTANT_MARKERS = (
    "自杀",
    "伤害",
    "不想活",
    "撑不住",
    "绝望",
    "持续",
    "每天",
    "今晚",
    "昨天",
    "明天",
    "小时",
    "分钟",
    "第一次",
    "又",
    "求助",
    "联系",
    "陪",
)


def _clip_preserving_edges(text: str, limit: int) -> tuple[str, bool]:
    normalized = re.sub(r"\s+", " ", text).strip()
    if len(normalized) <= limit:
        return normalized, False
    if limit <= 8:
        return normalized[:limit], True
    head = max(1, int((limit - 1) * 0.65))
    tail = max(1, limit - head - 1)
    return f"{normalized[:head]}…{normalized[-tail:]}", True


class ContextCompressor:
    def __init__(self, settings: Settings):
        self.recent_limit = settings.memory_recent_messages
        self.brief_limit = settings.memory_brief_max_chars
        self.history_char_limit = settings.memory_model_history_max_chars

    def _memory_brief(self, older: list[MemoryMessage]) -> str:
        if not older or self.brief_limit <= 0:
            return ""
        priority_indexes = {
            index
            for index, message in enumerate(older)
            if message.role == "student"
            and any(marker in message.content for marker in IMPORTANT_MARKERS)
        }
        priority_indexes.update(range(max(0, len(older) - 6), len(older)))
        selected = [older[index] for index in sorted(priority_indexes)]
        if not selected:
            selected = older[-4:]

        prefix = "较早对话摘要（脱敏、摘录式）："
        parts: list[str] = []
        used = len(prefix)
        for message in selected:
            role = "学生" if message.role == "student" else "助手"
            snippet, _ = _clip_preserving_edges(message.content, 160)
            part = f"{role}：{snippet}"
            remaining = self.brief_limit - used
            if remaining <= 1:
                break
            if len(part) > remaining:
                part, _ = _clip_preserving_edges(part, remaining)
            parts.append(part)
            used += len(part) + 1
        return (prefix + "；".join(parts))[: self.brief_limit]

    def _model_history(self, recent: list[MemoryMessage]) -> tuple[MemoryMessage, ...]:
        if not recent or self.history_char_limit <= 0:
            return ()
        selected: list[MemoryMessage] = []
        remaining = self.history_char_limit
        for message in reversed(recent[-self.recent_limit :]):
            role_cost = 12
            available = remaining - role_cost
            if available <= 0:
                break
            clipped, truncated = _clip_preserving_edges(message.content, available)
            selected.insert(
                0,
                replace(message, content=clipped, truncated=message.truncated or truncated),
            )
            remaining -= len(clipped) + role_cost
        return tuple(selected)

    def compress(
        self,
        session_public_id: str,
        messages: list[MemoryMessage],
        *,
        source: str,
        cache_hit: bool,
    ) -> MemoryContext:
        recent = messages[-self.recent_limit :]
        older = messages[: -self.recent_limit] if len(messages) > self.recent_limit else []
        return MemoryContext(
            session_public_id=session_public_id,
            memory_brief=self._memory_brief(older),
            model_history=self._model_history(recent),
            source=source,
            cache_hit=cache_hit,
            total_messages=len(messages),
            cached_messages=len(messages),
        )


class ShortTermMemoryService:
    def __init__(self, settings: Settings, redis_store):
        self.settings = settings
        self.redis = redis_store
        self.compressor = ContextCompressor(settings)

    def key(self, session_public_id: str) -> str:
        return f"{self.settings.short_term_memory_prefix}:{session_public_id}"

    @staticmethod
    def _serialize(message: MemoryMessage) -> str:
        return json.dumps(
            {
                "message_id": message.message_id,
                "role": message.role,
                "content": message.content,
                "created_at": message.created_at,
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _deserialize(value: str) -> MemoryMessage:
        data = json.loads(value)
        return MemoryMessage(
            message_id=str(data["message_id"]),
            role=str(data["role"]),
            content=str(data["content"]),
            created_at=str(data.get("created_at") or ""),
        )

    @staticmethod
    def _from_model(message: Message) -> MemoryMessage:
        return MemoryMessage(
            message_id=message.public_id,
            role=message.sender_type,
            content=message.redacted_content or redact_pii(message.content),
            created_at=message.created_at.isoformat(),
        )

    def _read_cache(self, key: str) -> list[MemoryMessage] | None:
        try:
            values = self.redis.lrange(key, 0, -1)
            if not values:
                return []
            messages = [self._deserialize(value) for value in values]
            self.redis.expire(key, self.settings.short_term_memory_ttl_seconds)
            return messages
        except (RedisError, OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            try:
                self.redis.delete(key)
            except Exception:  # noqa: BLE001 - cache failure must not block chat.
                pass
            return None

    def _replace_cache(self, key: str, messages: list[MemoryMessage]) -> None:
        try:
            self.redis.delete(key)
            if messages:
                self.redis.rpush(key, *(self._serialize(item) for item in messages))
                self.redis.ltrim(
                    key,
                    -self.settings.short_term_memory_max_messages,
                    -1,
                )
                self.redis.expire(key, self.settings.short_term_memory_ttl_seconds)
        except (RedisError, OSError):
            return

    def load(self, db, conversation: Conversation) -> MemoryContext:
        key = self.key(conversation.public_id)
        cached = self._read_cache(key)
        if cached:
            return self.compressor.compress(
                conversation.public_id,
                cached[-self.settings.short_term_memory_max_messages :],
                source="redis",
                cache_hit=True,
            )

        records = list(
            db.scalars(
                select(Message)
                .where(
                    Message.conversation_id == conversation.id,
                    Message.status == "completed",
                )
                .order_by(Message.id.desc())
                .limit(self.settings.short_term_memory_max_messages)
            )
        )
        messages = [self._from_model(item) for item in reversed(records)]
        self._replace_cache(key, messages)
        return self.compressor.compress(
            conversation.public_id,
            messages,
            source="mysql",
            cache_hit=False,
        )

    def append(self, session_public_id: str, message: Message) -> None:
        key = self.key(session_public_id)
        value = self._serialize(self._from_model(message))
        try:
            self.redis.rpush(key, value)
            self.redis.ltrim(
                key,
                -self.settings.short_term_memory_max_messages,
                -1,
            )
            self.redis.expire(key, self.settings.short_term_memory_ttl_seconds)
        except (RedisError, OSError):
            return
