from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal
import uuid

from pydantic import BaseModel, Field


DialogueType = Literal["general", "psychological_support", "safety_risk"]
RiskLevel = Literal["low", "medium", "high"]


class IntentAnalysis(BaseModel):
    dialogue_type: DialogueType
    topics: list[str] = Field(default_factory=list, max_length=8)
    needs_knowledge: bool = False
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=300)


class RiskAssessment(BaseModel):
    risk_level: RiskLevel
    confidence: float = Field(ge=0, le=1)
    emotion_summary: str = Field(min_length=1, max_length=300)
    reason: str = Field(min_length=1, max_length=300)
    immediate_danger: bool = False
    needs_safety_question: bool = False


class ResponseReview(BaseModel):
    approved: bool
    violations: list[str] = Field(default_factory=list, max_length=10)
    revision_guidance: str = Field(default="", max_length=400)


@dataclass
class ContextBundle:
    recent_summary: str
    knowledge: list[str]
    skills: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    memory_brief: str = ""
    model_history: list[dict[str, str]] = field(default_factory=list)


@dataclass
class AgentStepTrace:
    step_name: str
    status: str
    model_name: str | None = None
    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    summary: str = ""
    error_code: str | None = None


@dataclass(frozen=True)
class AgentTask:
    kind: str
    capability: str
    created_by: str
    parent_task_id: str | None = None
    input_artifact_ids: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    task_id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass(frozen=True)
class AgentArtifact:
    kind: str
    producer: str
    payload: Any
    task_id: str | None = None
    version: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)
    artifact_id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass(frozen=True)
class AgentMessage:
    sender: str
    recipient: str
    content: str
    artifact_ids: tuple[str, ...] = ()
    task_id: str | None = None
    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass(frozen=True)
class AgentEvent:
    type: str
    actor: str
    task_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    sequence: int = 0

    @property
    def agent(self) -> str:
        """Compatibility name consumed by AgentHarness trace events."""
        return self.actor


@dataclass(frozen=True)
class AgentTurnResult:
    messages: tuple[AgentMessage, ...] = ()
    artifacts: tuple[AgentArtifact, ...] = ()
    tasks: tuple[AgentTask, ...] = ()
    events: tuple[AgentEvent, ...] = ()
    close_task: bool = True


@dataclass
class RuntimeTrace:
    run_id: str
    status: str = "running"
    route: str = "unknown"
    final_risk_level: str | None = None
    provider: str = "unknown"
    model_calls: int = 0
    degraded: bool = False
    total_latency_ms: int = 0
    steps: list[AgentStepTrace] = field(default_factory=list)
    events: list[AgentEvent] = field(default_factory=list)
