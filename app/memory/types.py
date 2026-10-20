from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def is_stable_preference(text: str) -> bool:
    """Conservative gate for automatic communication-style injection, not a general preference classifier."""
    recurring = any(w in text for w in ("每次", "一直", "以后", "平时", "仍然", "之后", "长期"))
    communication = any(w in text for w in ("回复", "问", "交流", "讨论", "聊天", "建议", "追问", "节奏"))
    temporary = any(w in text for w in ("只是今天", "今天只", "这次", "不代表", "不是我对所有", "不是说我以后", "暂时"))
    return recurring and communication and not temporary


@dataclass(frozen=True)
class MemoryMessage:
    message_id: str
    role: str
    content: str
    created_at: str
    truncated: bool = False
    db_id: int = 0

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
    summary_version: int = 0
    summarized_until_message_id: int = 0
    recalled_memories: tuple[dict, ...] = ()
    background_incomplete: bool = False
    diagnostics: dict = field(default_factory=dict)

    @property
    def background_text(self) -> str:
        parts = []
        if self.diagnostics:
            parts.append("历史记忆是带时间的原话，不能把计划当成执行，也不能把他人经历当成用户经历。"
                         "未检索到记录只表示目前未找到，不能据此说用户从未提供；没有明确时间证据时禁止补写具体时间。")
        if self.memory_brief and self.history_compressed:
            parts.append("当前交流摘要：\n" + self.memory_brief)
        if self.recalled_memories:
            parts.append("相关历史记忆（过去的情况，不代表当前状态）：\n" + "\n".join(
                f"[{item['memory_id']} / {item['updated_at']}] {item.get('context_content', item['content'])}"
                for item in self.recalled_memories
            ))
        if self.background_incomplete:
            parts.append("部分历史背景暂不可用；没有检索到不等于用户没说过。只能说目前未找到记录，禁止说你从未提供。指代不明应确认。")
        if self.diagnostics.get("pending_corrections"):
            parts.append("以下用户更正尚待核对，优先遵循其原话，不把可能冲突的旧记忆当成当前事实：\n" +
                         "\n".join(self.diagnostics["pending_corrections"]))
        if self.diagnostics.get("needs_clarification"):
            parts.append("当前反馈的指代有歧义，先询问对应哪个办法或经历，不自行认定。")
        return "\n\n".join(parts)

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


class SummaryItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["concern", "change", "suggestion", "feedback", "preference", "pending"]
    text: str = Field(min_length=1, max_length=400)
    source_message_ids: list[str] = Field(min_length=1, max_length=30)
    memory_id: str | None = None


class MemoryOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["add", "update", "retract"] = "add"
    memory_id: str | None = None
    expected_version: int | None = None
    content: str = Field(min_length=1, max_length=600)
    type: Literal["concern", "support_feedback", "preference", "episode"]
    topics: list[str] = Field(default_factory=list, max_length=8)
    source_message_ids: list[str] = Field(min_length=1, max_length=30)
    stage: Literal["reported", "suggested", "intended", "attempted", "feedback"] = "reported"
    change: Literal["new", "correction", "evolution"] = "new"
    quote: str = Field(default="", max_length=600)
    subject: Literal["user", "other", "assistant", "uncertain"] = "uncertain"
    target_quote: str = Field(default="", max_length=600)


class MemoryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary_items: list[SummaryItem] = Field(default_factory=list, max_length=12)
    memory_operations: list[MemoryOperation] = Field(default_factory=list, max_length=30)
    needs_clarification: bool = False


class ExtractedFact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message_id: str
    quote: str = Field(min_length=1, max_length=600)
    type: Literal["concern", "support_feedback", "preference", "episode"]
    subject: Literal["user", "other", "assistant", "uncertain"]
    stage: Literal["reported", "suggested", "intended", "attempted", "feedback"]
    topics: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("stage", mode="before")
    @classmethod
    def normalize_reported_alias(cls, value):
        # A lexical alias only; never upgrade intended/suggested to attempted.
        return "reported" if value == "stated" else value


class FactExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    facts: list[ExtractedFact] = Field(default_factory=list, max_length=30)
    reviewed_message_ids: list[str]


class CorrectionTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    memory_id: str
    expected_version: int
    change: Literal["correction", "evolution"]


class CorrectionProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    corrections: list[CorrectionTarget] = Field(default_factory=list, max_length=12)
    needs_clarification: bool = False


class CorrectionVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    memory_id: str
    approved: bool


class CorrectionVerification(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decisions: list[CorrectionVerdict] = Field(max_length=12)
