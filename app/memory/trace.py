from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from typing import Any

from pydantic import BaseModel

from app.models import AgentRun, AgentRunTrace


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump()
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _dumps(value: Any) -> str:
    return json.dumps(_jsonable(value), ensure_ascii=False, default=str)


class AgentTraceService:
    """Persists audit memory; this data is never fed back into model prompts."""

    def persist(
        self,
        db,
        *,
        run: AgentRun,
        source_input: str,
        redacted_input: str,
        memory_context,
        plan,
        result,
    ) -> AgentRunTrace:
        artifacts = _jsonable(plan.blackboard.artifacts)
        artifacts.append({"kind": "memory_context", "payload": {
            "summary_version": memory_context.summary_version,
            "summarized_until_message_id": memory_context.summarized_until_message_id,
            "recalled_memories": memory_context.recalled_memories,
            "background_incomplete": memory_context.background_incomplete,
            "diagnostics": memory_context.diagnostics,
        }})
        trace = AgentRunTrace(
            run_id=run.id,
            source_input=source_input,
            redacted_input=redacted_input,
            memory_brief=memory_context.memory_brief,
            model_history_json=_dumps(memory_context.modelHistory),
            agent_steps_json=_dumps(result.trace.steps),
            retrieval_results_json=_dumps(plan.knowledge),
            assessment_json=_dumps(result.assessment),
            collaboration_events_json=_dumps(plan.blackboard.events),
            tasks_json=_dumps(plan.blackboard.tasks),
            artifacts_json=_dumps(artifacts),
            messages_json=_dumps(plan.blackboard.messages),
        )
        db.add(trace)
        return trace
