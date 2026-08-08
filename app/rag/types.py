from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class KnowledgeChunk:
    chunk_id: str
    source: str
    title: str
    section: str
    index: int
    content: str
    char_start: int
    char_end: int

    def chroma_metadata(self) -> dict[str, str | int]:
        return {
            "source": self.source,
            "title": self.title,
            "section": self.section,
            "chunk_index": self.index,
            "char_start": self.char_start,
            "char_end": self.char_end,
        }


@dataclass(frozen=True)
class RetrievalHit:
    chunk: KnowledgeChunk
    vector_score: float
    bm25_score: float
    normalized_vector_score: float
    normalized_bm25_score: float
    fused_score: float
    query_coverage: float
    phrase_score: float
    lexical_similarity: float
    rerank_score: float
    expanded_content: str | None = None
    expanded_chunk_ids: tuple[str, ...] = ()

    @property
    def content(self) -> str:
        return self.expanded_content or self.chunk.content

    @property
    def expanded(self) -> bool:
        return self.expanded_content is not None

    def with_expansion(self, content: str, chunk_ids: tuple[str, ...]) -> RetrievalHit:
        return replace(
            self,
            expanded_content=content,
            expanded_chunk_ids=chunk_ids,
        )

    def to_prompt_text(self) -> str:
        expansion = "；已扩展前后文" if self.expanded else ""
        header = (
            f"[知识来源：{self.chunk.source}；章节：{self.chunk.section}"
            f"；融合分：{self.fused_score:.3f}；重排分：{self.rerank_score:.3f}"
            f"{expansion}]"
        )
        return f"{header}\n{self.content}"


@dataclass(frozen=True)
class RetrievalStages:
    query: str
    vector: tuple[RetrievalHit, ...]
    bm25: tuple[RetrievalHit, ...]
    hybrid: tuple[RetrievalHit, ...]
    reranked: tuple[RetrievalHit, ...]
    final: tuple[RetrievalHit, ...]
