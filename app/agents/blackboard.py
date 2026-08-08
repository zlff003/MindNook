from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from dataclasses import replace

from app.agents.types import (
    AgentArtifact,
    AgentEvent,
    AgentMessage,
    AgentStepTrace,
    AgentTask,
    AgentTurnResult,
    RuntimeTrace,
)
from app.core.config import Settings
from app.services.model import ModelCallError, ModelMessage, ModelProvider, ModelResponse


class AgentBudgetExceeded(RuntimeError):
    pass


class CollaborationBlackboard:
    """Append-only collaboration state for one multi-agent run.

    Tasks are never mutated. Claiming and closing are represented by events, and
    artifacts/messages are appended as immutable records. Current state is derived
    from that history, preserving a reliable collaboration trace.
    """

    def __init__(self, settings: Settings, provider_name: str):
        self.started = time.perf_counter()
        self.deadline = self.started + settings.model_run_timeout_seconds
        self.max_calls = settings.model_max_calls_per_run
        self.max_rounds = settings.agent_max_rounds
        self.rounds = 0
        self.tasks: list[AgentTask] = []
        self.artifacts: list[AgentArtifact] = []
        self.messages: list[AgentMessage] = []
        self.events: list[AgentEvent] = []
        self.trace = RuntimeTrace(
            run_id=str(uuid.uuid4()),
            provider=provider_name,
            events=self.events,
        )

    def begin_round(self, actor: str, task: AgentTask) -> None:
        if self.rounds >= self.max_rounds:
            self.publish_event(
                "MAX_ROUNDS_EXCEEDED",
                "CoordinatorAgent",
                task_id=task.task_id,
                payload={"max_rounds": self.max_rounds},
            )
            self.trace.degraded = True
            raise AgentBudgetExceeded("collaboration round budget exhausted")
        self.rounds += 1
        self.publish_event(
            "ROUND_STARTED",
            actor,
            task_id=task.task_id,
            payload={"round": self.rounds, "kind": task.kind},
        )

    def publish_event(
        self,
        event_type: str,
        actor: str,
        *,
        task_id: str | None = None,
        payload: dict | None = None,
    ) -> AgentEvent:
        event = AgentEvent(
            sequence=len(self.events) + 1,
            type=event_type,
            actor=actor,
            task_id=task_id,
            payload=payload or {},
        )
        self.events.append(event)
        return event

    def add_task(self, task: AgentTask) -> AgentTask:
        self.tasks.append(task)
        self.publish_event(
            "TASK_CREATED",
            task.created_by,
            task_id=task.task_id,
            payload={"kind": task.kind, "capability": task.capability},
        )
        return task

    def create_root_task(self) -> AgentTask:
        return self.add_task(
            AgentTask(
                kind="coordinate_turn",
                capability="coordinate",
                created_by="CoordinatorAgent",
            )
        )

    def _last_task_event(self, task_id: str, event_type: str) -> AgentEvent | None:
        return next(
            (
                event
                for event in reversed(self.events)
                if event.task_id == task_id and event.type == event_type
            ),
            None,
        )

    def claimed_by(self, task: AgentTask) -> str | None:
        event = self._last_task_event(task.task_id, "TASK_CLAIMED")
        return event.actor if event else None

    def is_closed(self, task: AgentTask) -> bool:
        return self._last_task_event(task.task_id, "TASK_CLOSED") is not None

    def claim_task(self, agent: str, capabilities: set[str]) -> AgentTask | None:
        for task in self.tasks:
            if (
                task.capability in capabilities
                and not self.is_closed(task)
                and self.claimed_by(task) is None
            ):
                self.publish_event(
                    "TASK_CLAIMED",
                    agent,
                    task_id=task.task_id,
                    payload={"kind": task.kind, "capability": task.capability},
                )
                return task
        return None

    def merge_turn(
        self,
        task: AgentTask,
        agent: str,
        result: AgentTurnResult,
    ) -> None:
        if self.claimed_by(task) != agent:
            raise RuntimeError(f"{agent} cannot merge an unclaimed task")
        if self.is_closed(task):
            raise RuntimeError("cannot merge a closed task")

        for message in result.messages:
            record = replace(message, sender=agent, task_id=message.task_id or task.task_id)
            self.messages.append(record)
            self.publish_event(
                "MESSAGE_PUBLISHED",
                agent,
                task_id=task.task_id,
                payload={"recipient": record.recipient, "message_id": record.message_id},
            )
        for artifact in result.artifacts:
            record = replace(
                artifact,
                producer=agent,
                task_id=artifact.task_id or task.task_id,
                version=self.artifact_count(artifact.kind) + 1,
            )
            self.artifacts.append(record)
            self.publish_event(
                "ARTIFACT_PUBLISHED",
                agent,
                task_id=task.task_id,
                payload={
                    "kind": record.kind,
                    "artifact_id": record.artifact_id,
                    "version": record.version,
                },
            )
        for child_task in result.tasks:
            self.add_task(
                replace(
                    child_task,
                    created_by=agent,
                    parent_task_id=child_task.parent_task_id or task.task_id,
                )
            )
        for event in result.events:
            self.publish_event(
                event.type,
                agent,
                task_id=event.task_id or task.task_id,
                payload=event.payload,
            )
        if result.close_task:
            self.publish_event(
                "TASK_CLOSED",
                agent,
                task_id=task.task_id,
                payload={"kind": task.kind},
            )

    def artifact_count(self, kind: str) -> int:
        return sum(1 for artifact in self.artifacts if artifact.kind == kind)

    def latest_artifact(self, kind: str) -> AgentArtifact | None:
        return next(
            (artifact for artifact in reversed(self.artifacts) if artifact.kind == kind),
            None,
        )

    def require_artifact(self, kind: str) -> AgentArtifact:
        artifact = self.latest_artifact(kind)
        if not artifact:
            raise RuntimeError(f"required artifact is missing: {kind}")
        return artifact

    def has_task(self, kind: str, *, include_closed: bool = True) -> bool:
        return any(
            task.kind == kind and (include_closed or not self.is_closed(task))
            for task in self.tasks
        )

    def open_tasks(self) -> list[AgentTask]:
        return [task for task in self.tasks if not self.is_closed(task)]

    def _check_budget(self) -> None:
        if self.trace.model_calls >= self.max_calls:
            self.publish_event("BUDGET_EXHAUSTED", "CollaborationBlackboard")
            raise AgentBudgetExceeded("model call budget exhausted")
        if time.perf_counter() >= self.deadline:
            self.publish_event("DEADLINE_EXCEEDED", "CollaborationBlackboard")
            raise AgentBudgetExceeded("agent run deadline exceeded")

    def _record_step(self, step: AgentStepTrace, agent: str) -> None:
        self.trace.steps.append(step)
        self.publish_event(
            "AGENT_STEP_FINISHED",
            agent,
            payload={
                "step_name": step.step_name,
                "status": step.status,
                "model_name": step.model_name,
                "error_code": step.error_code,
            },
        )

    def local_step(
        self,
        name: str,
        summary: str,
        *,
        agent: str,
        status: str = "succeeded",
    ) -> None:
        self._record_step(
            AgentStepTrace(step_name=name, status=status, summary=summary),
            agent,
        )

    def call_model(
        self,
        provider: ModelProvider,
        *,
        agent: str,
        purpose: str,
        messages: list[ModelMessage],
        model: str,
        temperature: float,
        json_mode: bool,
    ) -> ModelResponse:
        self._check_budget()
        self.trace.model_calls += 1
        started = time.perf_counter()
        try:
            response = provider.complete(
                messages,
                model=model,
                temperature=temperature,
                json_mode=json_mode,
                purpose=purpose,
            )
            self._record_step(
                AgentStepTrace(
                    step_name=purpose,
                    status="succeeded",
                    model_name=response.model,
                    latency_ms=response.latency_ms,
                    input_tokens=response.input_tokens,
                    output_tokens=response.output_tokens,
                    summary="模型调用完成。",
                ),
                agent,
            )
            return response
        except (ModelCallError, AgentBudgetExceeded) as exc:
            self._record_step(
                AgentStepTrace(
                    step_name=purpose,
                    status="failed",
                    model_name=model,
                    latency_ms=max(1, int((time.perf_counter() - started) * 1000)),
                    summary="模型调用失败，协作运行时将执行降级策略。",
                    error_code=getattr(exc, "code", type(exc).__name__),
                ),
                agent,
            )
            self.trace.degraded = True
            raise

    def stream_model(
        self,
        provider: ModelProvider,
        *,
        agent: str,
        purpose: str,
        messages: list[ModelMessage],
        model: str,
        temperature: float,
    ) -> Iterator[str]:
        self._check_budget()
        self.trace.model_calls += 1
        started = time.perf_counter()
        output_parts: list[str] = []
        try:
            for delta in provider.stream(
                messages,
                model=model,
                temperature=temperature,
                purpose=purpose,
            ):
                output_parts.append(delta)
                yield delta
            self._record_step(
                AgentStepTrace(
                    step_name=purpose,
                    status="succeeded",
                    model_name=model,
                    latency_ms=max(1, int((time.perf_counter() - started) * 1000)),
                    input_tokens=max(1, sum(len(item.content) for item in messages) // 4),
                    output_tokens=max(1, len("".join(output_parts)) // 4),
                    summary="以流式模式生成候选回复。",
                ),
                agent,
            )
        except (ModelCallError, AgentBudgetExceeded) as exc:
            self._record_step(
                AgentStepTrace(
                    step_name=purpose,
                    status="failed",
                    model_name=model,
                    latency_ms=max(1, int((time.perf_counter() - started) * 1000)),
                    summary="流式模型调用失败。",
                    error_code=getattr(exc, "code", type(exc).__name__),
                ),
                agent,
            )
            self.trace.degraded = True
            raise

    def finish(self, route: str, risk_level: str | None, status: str = "succeeded") -> None:
        self.trace.route = route
        self.trace.final_risk_level = risk_level
        self.trace.status = status
        self.trace.total_latency_ms = max(
            1,
            int((time.perf_counter() - self.started) * 1000),
        )
