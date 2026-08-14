import unittest
from pathlib import Path

import chromadb
from chromadb.config import Settings as ChromaSettings

from app.core.config import Settings
from app.rag.evaluation import DEFAULT_DATASET, _rank_metrics, build_report, load_cases
from app.rag.service import HybridRAGService, build_retrieval_topics


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class RAGEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = load_cases(DEFAULT_DATASET)

    def test_labeled_dataset_has_expected_coverage(self):
        self.assertEqual(len(self.cases), 108)
        self.assertEqual(sum(case.evaluate_retrieval for case in self.cases), 89)
        self.assertEqual(sum(not case.expect_rag for case in self.cases), 19)
        self.assertEqual(sum(case.safety_critical for case in self.cases), 13)
        self.assertEqual(sum(bool(case.evidence_phrases) for case in self.cases), 9)
        self.assertEqual(len({case.category for case in self.cases}), 10)
        tags = {tag for case in self.cases for tag in case.challenge_tags}
        self.assertTrue(
            {
                "paraphrase",
                "mixed_intent",
                "long_context",
                "typo",
                "context_expansion",
                "hard_negative",
                "privacy_boundary",
            }.issubset(tags)
        )

    def test_evidence_phrases_exist_in_a_relevant_source(self):
        knowledge_dir = PROJECT_ROOT / "data" / "knowledge"
        for case in self.cases:
            relevant_texts = [
                (knowledge_dir / source).read_text(encoding="utf-8")
                for source in case.relevant_sources
            ]
            for phrase in case.evidence_phrases:
                self.assertTrue(
                    any(phrase in text for text in relevant_texts),
                    f"{case.case_id} 的证据短语不在相关来源中：{phrase}",
                )

    def test_rank_metrics_use_source_level_relevance(self):
        metrics = _rank_metrics(
            ["无关.md", "相关一.md", "相关二.md"],
            ("相关一.md", "相关二.md"),
            k=3,
        )
        self.assertEqual(metrics["hit_at_1"], 0.0)
        self.assertEqual(metrics["recall_at_3"], 1.0)
        self.assertAlmostEqual(metrics["mrr"], 0.5)

    def test_rank_metrics_limit_mrr_to_the_same_cutoff(self):
        metrics = _rank_metrics(
            ["无关一.md", "无关二.md", "无关三.md", "相关.md"],
            ("相关.md",),
            k=3,
        )
        self.assertEqual(metrics["mrr"], 0.0)

    def test_high_risk_topics_are_added_deterministically(self):
        topics = build_retrieval_topics(["情绪低落"], risk_level="RISK")
        self.assertIn("情绪低落", topics)
        self.assertIn("安全风险", topics)
        self.assertIn("现实求助", topics)

    def test_challenge_retrieval_report_includes_stage_evidence_metrics(self):
        settings = Settings(
            app_env="test",
            rag_knowledge_dir=str(PROJECT_ROOT / "data" / "knowledge"),
            rag_embedding_provider="hash",
            rag_embedding_dimension=128,
            rag_reranker_enabled=False,
        )
        client = chromadb.EphemeralClient(
            settings=ChromaSettings(anonymized_telemetry=False)
        )
        service = HybridRAGService(settings, client=client)

        report = build_report(service, settings, self.cases)

        self.assertEqual(report["dataset"]["sample_count"], 108)
        self.assertEqual(report["dataset"]["evidence_sample_count"], 9)
        self.assertIn("stage_comparisons", report["retrieval"])
        self.assertIn("challenge_breakdown", report["retrieval"])
        self.assertIn("rule", report["routing"])
        self.assertIn("runtime", report["routing"])
        self.assertEqual(
            report["retrieval"]["stages"]["final"]["evidence_query_count"],
            9,
        )


if __name__ == "__main__":
    unittest.main()
