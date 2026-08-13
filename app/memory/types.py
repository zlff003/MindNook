from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MemoryMessage:
    message_id: str
    role: str
    content: str
    created_at: str
    truncated: bool = False

    @property
    def model_role(self) -> str:
        return "assistant" if self.role == "assistant" else "user"

    def model_dict(self) -> dict[str, str]:
        return {"role": self.model_role, "content": self.content}


@dataclass(frozen=True)
class MemoryContext:
    session_public_id: str
    memory_brief: str
    model_history: tuple[MemoryMessage, ...]
    source: str
    cache_hit: bool
    total_messages: int
    cached_messages: int
    history_compressed: bool = False

    @property
    def memoryBrief(self) -> str:
        return self.memory_brief

    @property
    def modelHistory(self) -> list[dict[str, str]]:
        return [message.model_dict() for message in self.model_history]

    @classmethod
    def empty(cls, session_public_id: str = "standalone") -> "MemoryContext":
        return cls(
            session_public_id=session_public_id,
            memory_brief="",
            model_history=(),
            source="empty",
            cache_hit=False,
            total_messages=0,
            cached_messages=0,
            history_compressed=False,
        )
