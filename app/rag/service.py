from __future__ import annotations

import threading
from functools import lru_cache
from pathlib import Path

import chromadb
from chromadb.config import Settings as ChromaSettings

from app.core.config import Settings
from app.rag.text import (
    BM25Index,
    LocalChineseEmbedding,
    chunk_markdown_files,
    normalize_text,
    query_phrases,
    tokenize_chinese,
)
from app.rag.types import KnowledgeChunk, RetrievalHit, RetrievalStages


SAFETY_RETRIEVAL_TOPICS = ("安全风险", "危机支持", "当前安全", "现实求助")


def build_retrieval_topics(
    topics: list[str] | tuple[str, ...] | None,
    *,
    risk_level: str,
) -> list[str]:
    """Combine model topics with deterministic safety topics used in production/eval."""
    combined = list(topics or [])
    if risk_level == "RISK":
        combined.extend(SAFETY_RETRIEVAL_TOPICS)
    return list(dict.fromkeys(topic for topic in combined if topic))


def _normalize_scores(values: dict[str, float]) -> dict[str, float]:
    if not values:
        return {}
    minimum = min(values.values())
    maximum = max(values.values())
    if maximum == minimum:
        normalized = 1.0 if maximum > 0 else 0.0
        return {key: normalized for key in values}
    return {
        key: (value - minimum) / (maximum - minimum)
        for key, value in values.items()
    }


def _overlap_merge(left: str, right: str, maximum: int) -> str:
    maximum = min(maximum, len(left), len(right))
    for size in range(maximum, 0, -1):
        if left[-size:] == right[:size]:
            return left + right[size:]
    return f"{left}\n\n{right}"


class HybridRAGService:
    """Chroma vector recall + BM25 fusion + deterministic local reranking."""

    def __init__(
        self,
        settings: Settings,
        *,
        client=None,
        knowledge_dir: str | Path | None = None,
    ):
        self.settings = settings
        self.knowledge_dir = Path(knowledge_dir or settings.rag_knowledge_dir).resolve()
        self.embedding = LocalChineseEmbedding(settings.rag_embedding_dimension)
        self._client = client
        self._collection = None
        self._chunks: list[KnowledgeChunk] = []
        self._chunks_by_id: dict[str, KnowledgeChunk] = {}
        self._chunks_by_position: dict[tuple[str, int], KnowledgeChunk] = {}
        self._bm25 = BM25Index([])
        self._signature: tuple[tuple[str, int, int], ...] | None = None
        self._lock = threading.RLock()

    def _corpus_signature(self) -> tuple[tuple[str, int, int], ...]:
        if not self.knowledge_dir.exists():
            return ()
        return tuple(
            (path.name, path.stat().st_mtime_ns, path.stat().st_size)
            for path in sorted(self.knowledge_dir.glob("*.md"), key=lambda item: item.name)
        )

    def _build_client(self):
        chroma_settings = ChromaSettings(anonymized_telemetry=False)
        if self.settings.app_env == "test":
            return chromadb.EphemeralClient(settings=chroma_settings)
        path = Path(self.settings.rag_chroma_path).resolve()
        path.mkdir(parents=True, exist_ok=True)
        return chromadb.PersistentClient(path=str(path), settings=chroma_settings)

    def _ensure_index(self) -> None:
        signature = self._corpus_signature()
        with self._lock:
            if self._collection is not None and signature == self._signature:
                return
            self.knowledge_dir.mkdir(parents=True, exist_ok=True)
            chunks = chunk_markdown_files(
                self.knowledge_dir,
                chunk_size=self.settings.rag_chunk_size,
                overlap=self.settings.rag_chunk_overlap,
            )
            if self._client is None:
                self._client = self._build_client()
            collection_name = (
                f"{self.settings.rag_collection_name}_d"
                f"{self.settings.rag_embedding_dimension}"
            )
            self._collection = self._client.get_or_create_collection(
                name=collection_name,
                metadata={"hnsw:space": "cosine"},
            )

            desired_ids = {chunk.chunk_id for chunk in chunks}
            existing = self._collection.get(include=[])
            existing_ids = set(existing.get("ids") or [])
            stale_ids = sorted(existing_ids - desired_ids)
            if stale_ids:
                self._collection.delete(ids=stale_ids)

            for start in range(0, len(chunks), 100):
                batch = chunks[start : start + 100]
                self._collection.upsert(
                    ids=[chunk.chunk_id for chunk in batch],
                    documents=[chunk.content for chunk in batch],
                    metadatas=[chunk.chroma_metadata() for chunk in batch],
                    embeddings=self.embedding.embed_many(
                        [f"{chunk.title} {chunk.section} {chunk.content}" for chunk in batch],
                        document=True,
                    ),
                )

            self._chunks = chunks
            self._chunks_by_id = {chunk.chunk_id: chunk for chunk in chunks}
            self._chunks_by_position = {
                (chunk.source, chunk.index): chunk for chunk in chunks
            }
            self._bm25 = BM25Index(
                [
                    tokenize_chinese(
                        f"{chunk.title} {chunk.section} {chunk.content}"
                    )
                    for chunk in chunks
                ]
            )
            self._signature = signature

    def _vector_scores(self, query: str) -> dict[str, float]:
        if not self._chunks or self._collection is None:
            return {}
        count = min(len(self._chunks), self.settings.rag_candidate_k)
        result = self._collection.query(
            query_embeddings=[self.embedding.embed(query)],
            n_results=count,
            include=["distances"],
        )
        ids = (result.get("ids") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        return {
            chunk_id: max(0.0, 1.0 - float(distance))
            for chunk_id, distance in zip(ids, distances)
            if chunk_id in self._chunks_by_id
        }

    def _bm25_scores(self, query: str) -> dict[str, float]:
        values = self._bm25.scores(tokenize_chinese(query))
        ranked = sorted(
            zip(self._chunks, values),
            key=lambda item: item[1],
            reverse=True,
        )[: self.settings.rag_candidate_k]
        return {
            chunk.chunk_id: float(score)
            for chunk, score in ranked
            if score > 0
        }

    @staticmethod
    def _local_features(query: str, content: str) -> tuple[float, float, float]:
        query_tokens = set(tokenize_chinese(query))
        content_tokens = set(tokenize_chinese(content))
        intersection = query_tokens & content_tokens
        coverage = len(intersection) / max(1, len(query_tokens))
        lexical = len(intersection) / max(1, len(query_tokens | content_tokens))
        phrases = query_phrases(query)
        normalized_content = normalize_text(content)
        phrase_score = (
            sum(1 for phrase in phrases if phrase in normalized_content) / len(phrases)
            if phrases
            else 0.0
        )
        return coverage, phrase_score, lexical

    def _expand_top_hit(self, hit: RetrievalHit) -> RetrievalHit:
        chunk = hit.chunk
        neighbors = [
            self._chunks_by_position.get((chunk.source, chunk.index - 1)),
            chunk,
            self._chunks_by_position.get((chunk.source, chunk.index + 1)),
        ]
        selected = [item for item in neighbors if item is not None]
        if len(selected) <= 1:
            return hit
        content = selected[0].content
        maximum_overlap = max(8, self.settings.rag_chunk_overlap * 2)
        for item in selected[1:]:
            content = _overlap_merge(content, item.content, maximum_overlap)
        return hit.with_expansion(
            content,
            tuple(item.chunk_id for item in selected),
        )

    def _score_candidates(self, retrieval_query: str) -> list[RetrievalHit]:
        vector_scores = self._vector_scores(retrieval_query)
        bm25_scores = self._bm25_scores(retrieval_query)
        candidate_ids = set(vector_scores) | set(bm25_scores)
        if not candidate_ids:
            return []

        raw_vectors = {key: vector_scores.get(key, 0.0) for key in candidate_ids}
        raw_bm25 = {key: bm25_scores.get(key, 0.0) for key in candidate_ids}
        normalized_vectors = _normalize_scores(raw_vectors)
        normalized_bm25 = _normalize_scores(raw_bm25)
        vector_weight = max(0.0, self.settings.rag_vector_weight)
        bm25_weight = max(0.0, self.settings.rag_bm25_weight)
        weight_total = vector_weight + bm25_weight or 1.0
        vector_weight /= weight_total
        bm25_weight /= weight_total

        hits: list[RetrievalHit] = []
        for chunk_id in candidate_ids:
            chunk = self._chunks_by_id[chunk_id]
            fused = (
                vector_weight * normalized_vectors[chunk_id]
                + bm25_weight * normalized_bm25[chunk_id]
            )
            coverage, phrase_score, lexical = self._local_features(
                retrieval_query,
                f"{chunk.title} {chunk.section} {chunk.content}",
            )
            rerank = (
                0.65 * fused
                + 0.15 * coverage
                + 0.12 * phrase_score
                + 0.08 * lexical
            )
            hits.append(
                RetrievalHit(
                    chunk=chunk,
                    vector_score=raw_vectors[chunk_id],
                    bm25_score=raw_bm25[chunk_id],
                    normalized_vector_score=normalized_vectors[chunk_id],
                    normalized_bm25_score=normalized_bm25[chunk_id],
                    fused_score=fused,
                    query_coverage=coverage,
                    phrase_score=phrase_score,
                    lexical_similarity=lexical,
                    rerank_score=rerank,
                )
            )

        return hits

    def _final_results(
        self,
        ranked_hits: list[RetrievalHit],
        top_k: int,
    ) -> list[RetrievalHit]:
        top_k = max(1, top_k)
        selected: list[RetrievalHit] = []
        if ranked_hits:
            expanded_top = self._expand_top_hit(ranked_hits[0])
            selected.append(expanded_top)
            covered_ids = set(expanded_top.expanded_chunk_ids)
            covered_sources = {expanded_top.chunk.source}
            for hit in ranked_hits[1:]:
                # 第一名已经带上同文档邻块，后续名额优先留给不同来源，
                # 避免重复上下文挤掉另一个可能相关的主题。
                if (
                    hit.chunk.chunk_id in covered_ids
                    or hit.chunk.source in covered_sources
                ):
                    continue
                selected.append(hit)
                covered_sources.add(hit.chunk.source)
                if len(selected) >= top_k:
                    break
        return selected

    def retrieve_stages(
        self,
        query: str,
        *,
        topics: list[str] | None = None,
        limit: int | None = None,
    ) -> RetrievalStages:
        self._ensure_index()
        retrieval_query = " ".join(
            item for item in (query.strip(), " ".join(topics or [])) if item
        )
        if not self._chunks:
            return RetrievalStages(retrieval_query, (), (), (), (), ())
        hits = self._score_candidates(retrieval_query)
        vector = sorted(
            (hit for hit in hits if hit.vector_score > 0),
            key=lambda item: item.vector_score,
            reverse=True,
        )
        bm25 = sorted(
            (hit for hit in hits if hit.bm25_score > 0),
            key=lambda item: item.bm25_score,
            reverse=True,
        )
        hybrid = sorted(hits, key=lambda item: item.fused_score, reverse=True)
        reranked = sorted(
            hits,
            key=lambda item: (item.rerank_score, item.fused_score),
            reverse=True,
        )
        final = self._final_results(
            reranked,
            max(1, limit or self.settings.rag_top_k),
        )
        return RetrievalStages(
            query=retrieval_query,
            vector=tuple(vector),
            bm25=tuple(bm25),
            hybrid=tuple(hybrid),
            reranked=tuple(reranked),
            final=tuple(final),
        )

    def retrieve(
        self,
        query: str,
        *,
        topics: list[str] | None = None,
        limit: int | None = None,
    ) -> list[RetrievalHit]:
        return list(self.retrieve_stages(query, topics=topics, limit=limit).final)

    def status(self) -> dict:
        """Return an admin-safe snapshot of the currently indexed knowledge corpus."""
        self._ensure_index()
        with self._lock:
            source_counts: dict[str, int] = {}
            for chunk in self._chunks:
                source_counts[chunk.source] = source_counts.get(chunk.source, 0) + 1
            return {
                "status": "ready",
                "collection": self._collection.name if self._collection is not None else None,
                "source_count": len(source_counts),
                "chunk_count": len(self._chunks),
                "sources": [
                    {"name": source, "chunk_count": count}
                    for source, count in sorted(source_counts.items())
                ],
            }

    def rebuild(self) -> dict:
        """Force a deterministic rescan/upsert and return the resulting status."""
        with self._lock:
            self._signature = None
        self._ensure_index()
        return self.status()


@lru_cache(maxsize=8)
def _shared_service(
    app_env: str,
    knowledge_dir: str,
    chroma_path: str,
    collection_name: str,
    chunk_size: int,
    overlap: int,
    dimension: int,
    candidate_k: int,
    top_k: int,
    vector_weight: float,
    bm25_weight: float,
) -> HybridRAGService:
    settings = Settings(
        app_env=app_env,
        rag_knowledge_dir=knowledge_dir,
        rag_chroma_path=chroma_path,
        rag_collection_name=collection_name,
        rag_chunk_size=chunk_size,
        rag_chunk_overlap=overlap,
        rag_embedding_dimension=dimension,
        rag_candidate_k=candidate_k,
        rag_top_k=top_k,
        rag_vector_weight=vector_weight,
        rag_bm25_weight=bm25_weight,
    )
    return HybridRAGService(settings)


def get_rag_service(settings: Settings) -> HybridRAGService:
    return _shared_service(
        settings.app_env,
        str(Path(settings.rag_knowledge_dir).resolve()),
        str(Path(settings.rag_chroma_path).resolve()),
        settings.rag_collection_name,
        settings.rag_chunk_size,
        settings.rag_chunk_overlap,
        settings.rag_embedding_dimension,
        settings.rag_candidate_k,
        settings.rag_top_k,
        settings.rag_vector_weight,
        settings.rag_bm25_weight,
    )
