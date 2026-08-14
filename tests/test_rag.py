import tempfile
import unittest
from pathlib import Path

import chromadb
from chromadb.config import Settings as ChromaSettings

from app.agents.runtime import CoordinatorAgent
from app.core.config import Settings
from app.rag.service import HybridRAGService
from app.rag.text import chunk_markdown_files
from app.services.model import MockModelProvider


PROJECT_ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE_DIR = PROJECT_ROOT / "data" / "knowledge"


def rag_settings(**overrides) -> Settings:
    values = {
        "app_env": "test",
        "model_provider": "mock",
        "model_name": "test-model",
        "rag_knowledge_dir": str(KNOWLEDGE_DIR),
        "rag_chroma_path": "unused-in-tests",
        "rag_chunk_size": 512,
        "rag_chunk_overlap": 64,
        "rag_embedding_provider": "hash",
        "rag_embedding_dimension": 128,
        "rag_candidate_k": 10,
        "rag_top_k": 3,
        "rag_vector_weight": 0.45,
        "rag_bm25_weight": 0.55,
        "rag_reranker_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)


class RAGTests(unittest.TestCase):
    def test_cross_encoder_reranker_changes_hybrid_order(self):
        class FakeReranker:
            def score(self, query, documents, *, timeout):
                self.query = query
                self.documents = documents
                self.timeout = timeout
                return [0.05, 0.95, *([0.1] * (len(documents) - 2))]

            @staticmethod
            def status():
                return {
                    "enabled": True,
                    "provider": "fake",
                    "model": "fake-reranker",
                    "state": "ready",
                    "last_error": None,
                }

        client = chromadb.EphemeralClient(
            settings=ChromaSettings(anonymized_telemetry=False)
        )
        reranker = FakeReranker()
        settings = rag_settings(
            rag_reranker_enabled=True,
            rag_reranker_model_weight=0.9,
        )
        service = HybridRAGService(settings, client=client, reranker=reranker)

        stages = service.retrieve_stages("晚上一直睡不着")

        self.assertGreaterEqual(len(stages.hybrid), 2)
        self.assertEqual(
            stages.reranked[0].chunk.chunk_id,
            stages.hybrid[1].chunk.chunk_id,
        )
        self.assertNotEqual(
            stages.reranked[0].chunk.chunk_id,
            stages.hybrid[0].chunk.chunk_id,
        )
        self.assertTrue(all("\n" in document for document in reranker.documents))

    def test_reranker_failure_falls_back_to_hybrid(self):
        class FailingReranker:
            @staticmethod
            def score(query, documents, *, timeout):
                raise RuntimeError("reranker unavailable")

            @staticmethod
            def status():
                return {
                    "enabled": True,
                    "provider": "fake",
                    "model": "fake-reranker",
                    "state": "degraded",
                    "last_error": "reranker unavailable",
                }

        client = chromadb.EphemeralClient(
            settings=ChromaSettings(anonymized_telemetry=False)
        )
        service = HybridRAGService(
            rag_settings(rag_reranker_enabled=True),
            client=client,
            reranker=FailingReranker(),
        )

        stages = service.retrieve_stages("晚上一直睡不着")

        self.assertEqual(
            [hit.chunk.chunk_id for hit in stages.reranked],
            [hit.chunk.chunk_id for hit in stages.hybrid],
        )
        self.assertIn("RuntimeError", service.status()["reranker"]["fallback_reason"])

    def test_reranker_timeout_falls_back_to_hybrid(self):
        class TimedOutReranker:
            @staticmethod
            def score(query, documents, *, timeout):
                raise TimeoutError("reranker timeout")

            @staticmethod
            def status():
                return {
                    "enabled": True,
                    "provider": "fake",
                    "model": "fake-reranker",
                    "state": "warming_up",
                    "last_error": "reranker timeout",
                }

        client = chromadb.EphemeralClient(
            settings=ChromaSettings(anonymized_telemetry=False)
        )
        service = HybridRAGService(
            rag_settings(rag_reranker_enabled=True),
            client=client,
            reranker=TimedOutReranker(),
        )

        stages = service.retrieve_stages("晚上一直睡不着")

        self.assertEqual(
            [hit.chunk.chunk_id for hit in stages.reranked],
            [hit.chunk.chunk_id for hit in stages.hybrid],
        )
        self.assertEqual(service.status()["reranker"]["fallback_reason"], "timeout")

    def test_markdown_chunking_uses_512_with_64_overlap(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            content = "# 睡眠支持\n\n" + "睡不着时先降低刺激并保持安全。" * 100
            (directory / "睡眠支持.md").write_text(content, encoding="utf-8")

            chunks = chunk_markdown_files(directory, chunk_size=512, overlap=64)

            self.assertGreaterEqual(len(chunks), 3)
            self.assertTrue(all(len(chunk.content) <= 512 for chunk in chunks))
            self.assertTrue(all(chunk.source == "睡眠支持.md" for chunk in chunks))
            for previous, current in zip(chunks, chunks[1:]):
                self.assertEqual(previous.char_end - current.char_start, 64)

    def test_hybrid_retrieval_fuses_scores_and_expands_top_context(self):
        client = chromadb.EphemeralClient(
            settings=ChromaSettings(anonymized_telemetry=False)
        )
        service = HybridRAGService(rag_settings(), client=client)

        hits = service.retrieve("晚上一直睡不着")

        self.assertTrue(hits)
        self.assertEqual(hits[0].chunk.source, "睡眠困扰支持.md")
        self.assertTrue(hits[0].expanded)
        self.assertGreater(len(hits[0].expanded_chunk_ids), 1)
        self.assertFalse(
            set(hits[0].expanded_chunk_ids)
            & {hit.chunk.chunk_id for hit in hits[1:]}
        )
        self.assertGreaterEqual(hits[0].vector_score, 0)
        self.assertGreater(hits[0].bm25_score, 0)
        self.assertGreater(hits[0].query_coverage, 0)
        self.assertGreater(hits[0].phrase_score, 0)
        self.assertGreater(hits[0].lexical_similarity, 0)
        self.assertAlmostEqual(
            hits[0].fused_score,
            0.45 * hits[0].normalized_vector_score
            + 0.55 * hits[0].normalized_bm25_score,
        )
        self.assertIn("睡眠", hits[0].content)

    def test_context_agent_triggers_rag_for_support_conversation(self):
        class FakeHit:
            @staticmethod
            def to_prompt_text():
                return "[知识来源：测试知识.md]\n测试检索内容"

        class RecordingRAG:
            def __init__(self):
                self.queries = []

            def retrieve(self, query, *, topics=None, limit=None, risk_level="CHAT"):
                self.queries.append((query, topics, limit, risk_level))
                return [FakeHit()]

        rag = RecordingRAG()
        runtime = CoordinatorAgent(MockModelProvider(), rag_settings(), rag_service=rag)

        plan = runtime.plan("最近考试压力很大")

        self.assertEqual(len(rag.queries), 1)
        self.assertEqual(rag.queries[0][0], "最近考试压力很大")
        self.assertEqual(plan.knowledge, ["[知识来源：测试知识.md]\n测试检索内容"])
        context_events = [
            event for event in plan.events if event.type == "CONTEXT_PUBLISHED"
        ]
        self.assertEqual(
            context_events[-1].payload["retrieval_mode"],
            "chroma_bm25_hybrid",
        )

    def test_general_question_does_not_trigger_rag(self):
        class RecordingRAG:
            def __init__(self):
                self.calls = 0

            def retrieve(self, query, *, topics=None, limit=None, risk_level="CHAT"):
                self.calls += 1
                return []

        rag = RecordingRAG()
        runtime = CoordinatorAgent(MockModelProvider(), rag_settings(), rag_service=rag)

        plan = runtime.plan("请解释 Python 列表推导式")

        self.assertEqual(rag.calls, 0)
        self.assertEqual(plan.knowledge, [])

    def test_context_agent_falls_back_when_hybrid_rag_is_unavailable(self):
        class FailingRAG:
            @staticmethod
            def retrieve(query, *, topics=None, limit=None, risk_level="CHAT"):
                raise RuntimeError("rag unavailable")

        runtime = CoordinatorAgent(
            MockModelProvider(),
            rag_settings(),
            rag_service=FailingRAG(),
        )

        plan = runtime.plan("最近考试压力很大")

        self.assertTrue(plan.knowledge)
        self.assertTrue(plan.blackboard.trace.degraded)
        context_events = [
            event for event in plan.events if event.type == "CONTEXT_PUBLISHED"
        ]
        self.assertEqual(
            context_events[-1].payload["retrieval_mode"],
            "builtin_fallback",
        )

    def test_risk_retrieval_reserves_safety_source_in_top_k(self):
        client = chromadb.EphemeralClient(
            settings=ChromaSettings(anonymized_telemetry=False)
        )
        service = HybridRAGService(rag_settings(), client=client)

        hits = service.retrieve(
            "活着没意思，今晚只想让一切停下来",
            topics=["当前安全"],
            risk_level="RISK",
        )

        self.assertTrue(hits)
        self.assertEqual(hits[0].chunk.source, "安全风险与危机支持边界.md")


if __name__ == "__main__":
    unittest.main()
