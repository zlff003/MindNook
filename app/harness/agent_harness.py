from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field

from sqlalchemy import select

from app.agents.runtime import AgentBudgetExceeded, AgentResult, CoordinatorAgent
from app.memory.private import AgentPrivateMemory
from app.memory.service import ShortTermMemoryService
from app.memory.store import InMemoryRedisStore
from app.memory.trace import AgentTraceService
from app.models import (
    AgentCollaborationEvent,
    AgentRun,
    AgentStep,
    Conversation,
    Message,
    PsychologicalReport,
    User,
    utc_now,
)
from app.services.jobs import enqueue_report_jobs
from app.services.model import ModelCallError
from app.services.privacy import redact_pii


@dataclass(frozen=True)
class ChatCommand:
    student_public_id: str
    conversation_public_id: str
    content: str
    client_request_id: str


@dataclass(frozen=True)
class HarnessEvent:
    type: str
    payload: dict = field(default_factory=dict)
    public: bool = True


class AgentHarness:
    """Application orchestration around the HTTP-independent Agent Runtime.

    The Harness owns request/session resolution, privacy, persistence, tool plans,
    and trace output. The Runtime owns only multi-agent inference.
    """

    def __init__(
        self,
        session_factory,
        runtime: CoordinatorAgent,
        tool_dispatcher,
        memory_service: ShortTermMemoryService | None = None,
        agent_private_memory: AgentPrivateMemory | None = None,
        trace_service: AgentTraceService | None = None,
    ):
        self.session_factory = session_factory
        self.runtime = runtime
        self.tool_dispatcher = tool_dispatcher
        fallback_store = InMemoryRedisStore()
        self.memory_service = memory_service or ShortTermMemoryService(
            runtime.settings,
            fallback_store,
        )
        self.agent_private_memory = agent_private_memory or AgentPrivateMemory(
            runtime.settings,
            fallback_store,
        )
        self.trace_service = trace_service or AgentTraceService()

    @staticmethod
    def _public_chunks(text: str, size: int = 28) -> list[str]:
        return [text[index : index + size] for index in range(0, len(text), size)]

    @staticmethod
    def _tool_plan(result: AgentResult) -> list[str]:
        if result.assessment.risk_level == "CHAT":
            return []
        tools = ["ledger_write"]
        if result.assessment.risk_level == "RISK":
            tools.append("case_create")
            tools.append("alert_send")
        return tools

    @staticmethod
    def _persist_trace(
        db,
        result: AgentResult,
        conversation: Conversation,
        user_message: Message,
    ) -> AgentRun:
        trace = result.trace
        run = AgentRun(
            public_id=trace.run_id,
            conversation_id=conversation.id,
            source_message_id=user_message.id,
            status=trace.status,
            risk_level=trace.risk_level,
            provider=trace.provider,
            model_calls=trace.model_calls,
            degraded=trace.degraded,
            total_latency_ms=trace.total_latency_ms,
        )
        db.add(run)
        db.flush()
        db.add_all(
            [
                AgentStep(
                    run_id=run.id,
                    sequence=index,
                    step_name=step.step_name,
                    status=step.status,
                    model_name=step.model_name,
                    latency_ms=step.latency_ms,
                    input_tokens=step.input_tokens,
                    output_tokens=step.output_tokens,
                    summary=step.summary,
                    error_code=step.error_code,
                )
                for index, step in enumerate(trace.steps, start=1)
            ]
        )
        db.add_all(
            [
                AgentCollaborationEvent(
                    run_id=run.id,
                    sequence=event.sequence,
                    event_type=event.type,
                    actor=event.actor,
                    task_public_id=event.task_id,
                    payload_json=json.dumps(
                        event.payload,
                        ensure_ascii=False,
                        default=str,
                    ),
                )
                for event in trace.events
            ]
        )
        return run

    def _resolve_session(self, db, command: ChatCommand) -> tuple[User, Conversation]:
        student = db.scalar(
            select(User).where(
                User.public_id == command.student_public_id,
                User.role == "student",
                User.status == "active",
            )
        )
        if not student:
            raise LookupError("student session is not available")
        conversation = db.scalar(
            select(Conversation).where(
                Conversation.public_id == command.conversation_public_id,
                Conversation.student_id == student.id,
            )
        )
        if not conversation:
            raise LookupError("conversation is not available")
        return student, conversation

    def _duplicate_events(
        self,
        db,
        command: ChatCommand,
        conversation: Conversation,
    ) -> list[HarnessEvent] | None:
        user_message = db.scalar(
            select(Message).where(
                Message.conversation_id == conversation.id,
                Message.client_request_id == command.client_request_id,
            )
        )
        if not user_message:
            return None
        reply = db.scalar(
            select(Message).where(
                Message.reply_to_message_id == user_message.id,
                Message.sender_type == "assistant",
            )
        )
        events = [
            HarnessEvent(
                "accepted",
                {
                    "client_request_id": command.client_request_id,
                    "duplicate": True,
                },
            )
        ]
        if not reply:
            events.append(
                HarnessEvent(
                    "error",
                    {
                        "code": "CHAT_REQUEST_IN_PROGRESS",
                        "message": "该请求仍在处理中，请稍后重试。",
                        "retryable": True,
                    },
                )
            )
            return events
        events.extend(
            HarnessEvent("delta", {"text": chunk})
            for chunk in self._public_chunks(reply.content)
        )
        events.append(
            HarnessEvent(
                "completed",
                {
                    "message_id": reply.public_id,
                    "conversation_id": conversation.public_id,
                    "duplicate": True,
                },
            )
        )
        return events

    def run(self, command: ChatCommand) -> Iterator[HarnessEvent]:
        db = self.session_factory()
        user_message: Message | None = None
        try:
            student, conversation = self._resolve_session(db, command)
            duplicate_events = self._duplicate_events(db, command, conversation)
            if duplicate_events is not None:
                yield from duplicate_events
                return

            yield HarnessEvent(
                "accepted",
                {
                    "client_request_id": command.client_request_id,
                    "duplicate": False,
                },
            )

            redacted = redact_pii(command.content.strip())
            memory_context = self.memory_service.load(
                db,
                conversation,
                current_input=redacted,
            )
            private_memory = self.agent_private_memory.load_for_agents(
                conversation.public_id
            )
            user_message = Message(
                conversation_id=conversation.id,
                sender_type="student",
                content=command.content.strip(),
                redacted_content=redacted,
                client_request_id=command.client_request_id,
                status="generating",
            )
            db.add(user_message)
            conversation.updated_at = utc_now()
            if conversation.title == "新的对话":
                conversation.title = command.content.strip().replace("\n", " ")[:24]
            db.commit()

            yield HarnessEvent("status", {"stage": "reasoning"})
            plan = self.runtime.plan(
                redacted,
                memory_context=memory_context,
                private_memory=private_memory,
            )
            emitted_runtime_events = 0
            for runtime_event in plan.events:
                yield HarnessEvent(
                    "runtime_event",
                    {
                        "sequence": runtime_event.sequence,
                        "type": runtime_event.type,
                        "agent": runtime_event.agent,
                        "payload": runtime_event.payload,
                    },
                    public=False,
                )
                emitted_runtime_events += 1
            yield HarnessEvent(
                "runtime_plan",
                {
                    "intent": plan.intent.model_dump(),
                    "risk_level": plan.risk.risk_level,
                    "knowledge_count": len(plan.knowledge),
                    "response_message_count": len(plan.response_messages),
                    "step_count": len(plan.steps),
                    "task_count": len(plan.blackboard.tasks),
                    "artifact_count": len(plan.blackboard.artifacts),
                    "message_count": len(plan.blackboard.messages),
                    "event_count": len(plan.blackboard.events),
                    "memory": {
                        "source": memory_context.source,
                        "cache_hit": memory_context.cache_hit,
                        "total_messages": memory_context.total_messages,
                        "memory_brief_length": len(memory_context.memory_brief),
                        "model_history_count": len(memory_context.model_history),
                    },
                },
                public=False,
            )

            yield HarnessEvent("status", {"stage": "generating"})
            candidate_parts: list[str] = []
            received_characters = 0
            try:
                for delta in self.runtime.stream_response(plan):
                    candidate_parts.append(delta)
                    received_characters += len(delta)
                    yield HarnessEvent(
                        "model_progress",
                        {"received_characters": received_characters},
                        public=False,
                    )
                candidate = "".join(candidate_parts).strip()
                for runtime_event in plan.events[emitted_runtime_events:]:
                    yield HarnessEvent(
                        "runtime_event",
                        {
                            "sequence": runtime_event.sequence,
                            "type": runtime_event.type,
                            "agent": runtime_event.agent,
                            "payload": runtime_event.payload,
                        },
                        public=False,
                    )
                    emitted_runtime_events += 1
                yield HarnessEvent("status", {"stage": "checking"})
                result = self.runtime.finalize_response(plan, candidate)
            except (ModelCallError, AgentBudgetExceeded):
                result = self.runtime.generation_failed(plan)

            for runtime_event in plan.events[emitted_runtime_events:]:
                yield HarnessEvent(
                    "runtime_event",
                    {
                        "sequence": runtime_event.sequence,
                        "type": runtime_event.type,
                        "agent": runtime_event.agent,
                        "payload": runtime_event.payload,
                    },
                    public=False,
                )
                emitted_runtime_events += 1

            assistant_message = Message(
                conversation_id=conversation.id,
                sender_type="assistant",
                content=result.response,
                redacted_content=result.response,
                reply_to_message_id=user_message.id,
                status="completed",
            )
            db.add(assistant_message)
            user_message.status = "completed"
            agent_run = self._persist_trace(db, result, conversation, user_message)
            self.trace_service.persist(
                db,
                run=agent_run,
                source_input=command.content.strip(),
                redacted_input=redacted,
                memory_context=memory_context,
                plan=plan,
                result=result,
            )
            tool_plan = self._tool_plan(result)

            report: PsychologicalReport | None = None
            if result.assessment.risk_level != "CHAT":
                report = PsychologicalReport(
                    conversation_id=conversation.id,
                    source_message_id=user_message.id,
                    student_id=student.id,
                    emotion_summary=result.assessment.emotion_summary,
                    risk_level=result.assessment.risk_level,
                    confidence=result.assessment.confidence,
                    reason_summary=result.assessment.reason_summary,
                )
                db.add(report)
                db.flush()
                report.handoff_summary = self.runtime.skill_registry.render_handoff_summary(
                    report_id=report.public_id,
                    student_identity=f"{student.username}（{student.public_id}）",
                    risk_level=report.risk_level,
                    emotion_summary=report.emotion_summary,
                    confidence=report.confidence,
                    reason_summary=report.reason_summary,
                    student_expression=command.content.strip(),
                )
                enqueue_report_jobs(
                    db,
                    report,
                    tool_plan,
                    max_attempts=self.runtime.settings.tool_worker_max_attempts,
                )
            db.commit()
            self.memory_service.append(conversation.public_id, user_message)
            self.memory_service.append(conversation.public_id, assistant_message)
            self.agent_private_memory.record_run(
                conversation.public_id,
                plan,
                result,
            )
            if tool_plan:
                self.tool_dispatcher.dispatch()

            for chunk in self._public_chunks(result.response):
                yield HarnessEvent("delta", {"text": chunk})
            yield HarnessEvent(
                "completed",
                {
                    "message_id": assistant_message.public_id,
                    "conversation_id": conversation.public_id,
                    "duplicate": False,
                },
            )
            yield HarnessEvent(
                "trace",
                {
                    "run_id": result.trace.run_id,
                    "risk_level": result.trace.risk_level,
                    "degraded": result.trace.degraded,
                    "tool_plan": tool_plan,
                    "report_id": report.public_id if report else None,
                    "collaboration": {
                        "tasks": len(plan.blackboard.tasks),
                        "artifacts": len(plan.blackboard.artifacts),
                        "messages": len(plan.blackboard.messages),
                        "events": len(plan.blackboard.events),
                        "rounds": plan.blackboard.rounds,
                    },
                    "memory": {
                        "source": memory_context.source,
                        "cache_hit": memory_context.cache_hit,
                        "memoryBrief": memory_context.memory_brief,
                        "modelHistory": memory_context.modelHistory,
                    },
                    "collaboration_events": [
                        {
                            "sequence": event.sequence,
                            "type": event.type,
                            "actor": event.actor,
                            "task_id": event.task_id,
                        }
                        for event in plan.blackboard.events
                    ],
                    "steps": [
                        {
                            "name": step.step_name,
                            "status": step.status,
                            "error_code": step.error_code,
                        }
                        for step in result.trace.steps
                    ],
                },
                public=False,
            )
        except LookupError:
            db.rollback()
            yield HarnessEvent(
                "error",
                {
                    "code": "CHAT_CONTEXT_NOT_FOUND",
                    "message": "会话不存在或无权访问。",
                    "retryable": False,
                },
            )
        except Exception:  # noqa: BLE001 - persist failure and expose a stable event.
            db.rollback()
            if user_message and user_message.id:
                failed_message = db.get(Message, user_message.id)
                if failed_message:
                    failed_message.status = "failed"
                    db.commit()
            yield HarnessEvent(
                "error",
                {
                    "code": "CHAT_PROCESSING_FAILED",
                    "message": "服务暂时不可用，请稍后重试。",
                    "retryable": True,
                },
            )
        finally:
            db.close()
