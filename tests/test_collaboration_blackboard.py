import unittest
from dataclasses import FrozenInstanceError

from app.agents.blackboard import CollaborationBlackboard
from app.agents.runtime import CoordinatorAgent
from app.agents.types import (
    AgentArtifact,
    AgentEvent,
    AgentMessage,
    AgentTask,
    AgentTurnResult,
)
from app.core.config import Settings
from app.services.model import MockModelProvider, ModelResponse


def collaboration_settings(**overrides) -> Settings:
    values = {
        "app_env": "test",
        "model_provider": "mock",
        "model_name": "test-model",
        "model_max_calls_per_run": 7,
        "model_run_timeout_seconds": 10,
        "agent_max_rounds": 16,
    }
    values.update(overrides)
    return Settings(**values)


class UnsafeProposalProvider(MockModelProvider):
    def complete(self, messages, *, model, temperature, json_mode, purpose):
        if purpose == "response_generation":
            return ModelResponse(
                content="你的风险等级是高风险，这是后台报告。",
                model=model,
                latency_ms=1,
            )
        return super().complete(
            messages,
            model=model,
            temperature=temperature,
            json_mode=json_mode,
            purpose=purpose,
        )


class CollaborationBlackboardTests(unittest.TestCase):
    def test_turn_merge_is_append_only_and_task_state_comes_from_events(self):
        board = CollaborationBlackboard(collaboration_settings(), "mock")
        root = board.create_root_task()
        self.assertEqual(board.claim_task("CoordinatorAgent", {"coordinate"}), root)

        intent = AgentArtifact("intent", "UnderstandingAgent", {"route": "general"})
        child = AgentTask(
            kind="collect_context",
            capability="context",
            created_by="UnderstandingAgent",
        )
        board.merge_turn(
            root,
            "CoordinatorAgent",
            AgentTurnResult(
                artifacts=(intent,),
                messages=(
                    AgentMessage(
                        "CoordinatorAgent",
                        "ContextAgent",
                        "请准备上下文。",
                    ),
                ),
                tasks=(child,),
                events=(AgentEvent("WORK_DERIVED", "CoordinatorAgent"),),
                close_task=False,
            ),
        )

        self.assertEqual(len(board.tasks), 2)
        self.assertEqual(len(board.artifacts), 1)
        self.assertEqual(len(board.messages), 1)
        self.assertFalse(board.is_closed(root))
        self.assertEqual(board.claimed_by(root), "CoordinatorAgent")
        event_types = [event.type for event in board.events]
        self.assertIn("TASK_CREATED", event_types)
        self.assertIn("TASK_CLAIMED", event_types)
        self.assertIn("ARTIFACT_PUBLISHED", event_types)
        self.assertIn("MESSAGE_PUBLISHED", event_types)
        self.assertIn("WORK_DERIVED", event_types)

        claimed_child = board.claim_task("ContextAgent", {"context"})
        self.assertEqual(claimed_child.task_id, child.task_id)
        board.merge_turn(
            claimed_child,
            "ContextAgent",
            AgentTurnResult(close_task=True),
        )
        self.assertTrue(board.is_closed(claimed_child))
        with self.assertRaises(FrozenInstanceError):
            root.kind = "mutated"

    def test_agents_claim_capabilities_and_publish_collaboration_records(self):
        runtime = CoordinatorAgent(MockModelProvider(), collaboration_settings())
        plan = runtime.plan("最近考试压力很大，晚上睡不着", [])
        board = plan.blackboard

        artifact_kinds = [artifact.kind for artifact in board.artifacts]
        self.assertIn("intent", artifact_kinds)
        self.assertIn("risk", artifact_kinds)
        self.assertIn("context", artifact_kinds)
        self.assertIn("response_prompt", artifact_kinds)
        self.assertNotIn("response_proposal", artifact_kinds)

        claims = {
            event.payload.get("kind"): event.actor
            for event in board.events
            if event.type == "TASK_CLAIMED"
        }
        self.assertEqual(claims["understand_input"], "UnderstandingAgent")
        self.assertEqual(claims["assess_risk"], "SafetyAgent")
        self.assertEqual(claims["collect_context"], "ContextAgent")
        self.assertEqual(claims["prepare_response"], "ResponseAgent")

        candidate = "".join(runtime.stream_response(plan))
        result = runtime.finalize_response(plan, candidate)

        final_kinds = [artifact.kind for artifact in board.artifacts]
        self.assertIn("response_proposal", final_kinds)
        self.assertIn("safety_review", final_kinds)
        self.assertIn("final_response", final_kinds)
        self.assertTrue(board.is_closed(plan.root_task))
        self.assertIn("FINAL_ADOPTED", [event.type for event in board.events])
        self.assertTrue(
            any(
                message.sender == "ResponseAgent"
                and message.recipient == "SafetyAgent"
                for message in board.messages
            )
        )
        self.assertEqual(result.response, board.require_artifact("final_response").payload)

    def test_high_risk_publishes_safety_override(self):
        runtime = CoordinatorAgent(MockModelProvider(), collaboration_settings())
        result = runtime.run("我想自杀，现在完全撑不住了", [])

        overrides = [
            event
            for event in result.trace.events
            if event.type == "SAFETY_OVERRIDE"
        ]
        self.assertTrue(overrides)
        self.assertEqual(result.assessment.risk_level, "RISK")
        self.assertIn("不要独处", result.response)

    def test_repair_appends_new_proposals_and_reviews_without_overwrite(self):
        runtime = CoordinatorAgent(UnsafeProposalProvider(), collaboration_settings())
        plan = runtime.plan("最近压力很大", [])
        candidate = "".join(runtime.stream_response(plan))
        result = runtime.finalize_response(plan, candidate)

        proposals = [
            artifact
            for artifact in plan.blackboard.artifacts
            if artifact.kind == "response_proposal"
        ]
        reviews = [
            artifact
            for artifact in plan.blackboard.artifacts
            if artifact.kind == "safety_review"
        ]
        self.assertEqual([item.version for item in proposals], [1, 2])
        self.assertEqual([item.version for item in reviews], [1, 2])
        self.assertNotEqual(proposals[0].artifact_id, proposals[1].artifact_id)
        self.assertNotIn("风险等级", result.response)
        self.assertIn("REPAIR_REQUESTED", [event.type for event in result.trace.events])

    def test_max_rounds_terminates_with_coordinator_fallback(self):
        runtime = CoordinatorAgent(
            MockModelProvider(),
            collaboration_settings(agent_max_rounds=1),
        )
        result = runtime.run("最近压力很大", [])

        self.assertTrue(result.trace.degraded)
        self.assertTrue(result.response)
        self.assertIn(
            "MAX_ROUNDS_EXCEEDED",
            [event.type for event in result.trace.events],
        )
        self.assertIn("FINAL_ADOPTED", [event.type for event in result.trace.events])


if __name__ == "__main__":
    unittest.main()
