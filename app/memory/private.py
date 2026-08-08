from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from typing import Any

from pydantic import BaseModel
from redis.exceptions import RedisError

from app.core.config import Settings


AGENT_NAMES = (
    "UnderstandingAgent",
    "SafetyAgent",
    "ContextAgent",
    "ResponseAgent",
)


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


class AgentPrivateMemory:
    def __init__(self, settings: Settings, redis_store):
        self.settings = settings
        self.redis = redis_store

    @staticmethod
    def key(agent_name: str, session_public_id: str) -> str:
        return f"agent:{agent_name}:{session_public_id}"

    def load_for_agents(
        self,
        session_public_id: str,
        agent_names: tuple[str, ...] = AGENT_NAMES,
    ) -> dict[str, tuple[str, ...]]:
        result: dict[str, tuple[str, ...]] = {}
        for agent_name in agent_names:
            key = self.key(agent_name, session_public_id)
            try:
                values = self.redis.lrange(key, 0, -1)
                self.redis.expire(key, self.settings.agent_private_memory_ttl_seconds)
                result[agent_name] = tuple(str(value) for value in values)
            except (RedisError, OSError):
                result[agent_name] = ()
        return result

    def append(self, agent_name: str, session_public_id: str, payload: dict) -> None:
        key = self.key(agent_name, session_public_id)
        value = json.dumps(_jsonable(payload), ensure_ascii=False, default=str)
        try:
            self.redis.rpush(key, value)
            self.redis.ltrim(
                key,
                -self.settings.agent_private_memory_max_entries,
                -1,
            )
            self.redis.expire(key, self.settings.agent_private_memory_ttl_seconds)
        except (RedisError, OSError):
            return

    def record_run(self, session_public_id: str, plan, result) -> None:
        intent = plan.blackboard.latest_artifact("intent")
        risk = plan.blackboard.latest_artifact("risk")
        reviews = [
            artifact
            for artifact in plan.blackboard.artifacts
            if artifact.kind == "safety_review"
        ]
        adoption = next(
            (
                event
                for event in reversed(plan.blackboard.events)
                if event.type == "FINAL_ADOPTED"
            ),
            None,
        )
        if intent:
            self.append(
                "UnderstandingAgent",
                session_public_id,
                {"kind": "intent", "value": intent.payload},
            )
        if risk or reviews:
            self.append(
                "SafetyAgent",
                session_public_id,
                {
                    "kind": "safety_state",
                    "risk": risk.payload if risk else None,
                    "latest_review": reviews[-1].payload if reviews else None,
                },
            )
        context = plan.blackboard.latest_artifact("context")
        if context:
            self.append(
                "ContextAgent",
                session_public_id,
                {
                    "kind": "context_strategy",
                    "knowledge_count": len(context.payload.knowledge),
                    "skills": context.payload.skills,
                },
            )
        self.append(
            "ResponseAgent",
            session_public_id,
            {
                "kind": "response_strategy",
                "route": result.assessment.dialogue_type,
                "risk_level": result.assessment.risk_level,
                "adoption_source": adoption.payload.get("source") if adoption else None,
            },
        )
