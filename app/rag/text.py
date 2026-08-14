from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from pathlib import Path

from app.rag.types import KnowledgeChunk


CJK_RUN = re.compile(r"[\u3400-\u9fff]+")
ASCII_WORD = re.compile(r"[a-z0-9]+")
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)

SEMANTIC_GROUPS = {
    "压力": ("压力", "学业", "考试", "作业", "任务", "拖延", "负担"),
    "焦虑": ("焦虑", "紧张", "担心", "害怕", "恐慌", "心慌"),
    "睡眠": ("睡眠", "失眠", "睡不着", "早醒", "熬夜", "作息"),
    "低落": ("低落", "难过", "孤独", "无助", "绝望", "情绪"),
    "关系": ("关系", "室友", "同学", "朋友", "家人", "冲突", "沟通"),
    "适应": ("适应", "校园", "新生", "转学", "陌生", "融入"),
    "安全": ("安全", "自伤", "自杀", "伤害", "危机", "求助", "陪伴"),
    "求助": ("咨询", "求助", "辅导员", "心理中心", "支持", "转介"),
}


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", "", text.lower())


def tokenize_chinese(text: str) -> list[str]:
    """Dependency-free Chinese tokens shared by BM25 and the local reranker."""
    lowered = text.lower()
    tokens = ASCII_WORD.findall(lowered)
    for run in CJK_RUN.findall(lowered):
        if len(run) == 1:
            tokens.append(run)
            continue
        tokens.extend(run[index : index + 2] for index in range(len(run) - 1))
        if len(run) >= 3:
            tokens.extend(run[index : index + 3] for index in range(len(run) - 2))
    return tokens


def query_phrases(text: str) -> set[str]:
    phrases: set[str] = set()
    for run in CJK_RUN.findall(text.lower()):
        for size in (2, 3, 4):
            if len(run) >= size:
                phrases.update(
                    run[index : index + size]
                    for index in range(len(run) - size + 1)
                )
    phrases.update(word for word in ASCII_WORD.findall(text.lower()) if len(word) >= 3)
    return phrases


class LocalChineseEmbedding:
    """Small offline embedding based on hashed lexical and semantic-anchor features."""

    def __init__(self, dimension: int = 384):
        if dimension < 64:
            raise ValueError("embedding dimension must be at least 64")
        self.dimension = dimension

    def _features(self, text: str) -> Counter[str]:
        features: Counter[str] = Counter(tokenize_chinese(text))
        if not features:
            features[normalize_text(text) or "空文本"] = 1
        return features

    def embed(self, text: str, *, document: bool = False) -> list[float]:
        vector = [0.0] * self.dimension
        normalized = normalize_text(text)
        semantic_dimensions = len(SEMANTIC_GROUPS)
        for index, keywords in enumerate(SEMANTIC_GROUPS.values()):
            matches = sum(normalized.count(keyword) for keyword in keywords)
            threshold = 2 if document else 1
            if matches >= threshold:
                # 文档中同一主题出现得越稳定，语义锚点越强；查询侧保持固定权重，
                # 避免只靠一个宽泛词就把无关文档推到前面。
                vector[index] = min(16.0, 6.0 + 2.0 * matches) if document else 8.0

        lexical_dimensions = self.dimension - semantic_dimensions
        for feature, count in self._features(text).items():
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            value = int.from_bytes(digest, "big")
            index = semantic_dimensions + value % lexical_dimensions
            sign = 1.0 if value & (1 << 63) else -1.0
            vector[index] += sign * (1.0 + math.log(max(1, count)))
        norm = math.sqrt(sum(item * item for item in vector)) or 1.0
        return [item / norm for item in vector]

    def embed_many(
        self,
        texts: list[str],
        *,
        document: bool = False,
    ) -> list[list[float]]:
        return [self.embed(text, document=document) for text in texts]


class FastEmbedChineseEmbedding:
    """Production Chinese embedding backed by a maintained ONNX model runtime."""

    def __init__(
        self,
        model_name: str = "BAAI/bge-small-zh-v1.5",
        *,
        cache_dir: str | Path | None = None,
        expected_dimension: int = 512,
    ):
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:  # pragma: no cover - dependency contract
            raise RuntimeError(
                "fastembed is required for the configured RAG embedding provider"
            ) from exc
        self.model_name = model_name
        self.dimension = TextEmbedding.get_embedding_size(model_name)
        if self.dimension != expected_dimension:
            raise ValueError(
                f"embedding dimension mismatch: model={self.dimension}, "
                f"configured={expected_dimension}"
            )
        self._model = TextEmbedding(
            model_name=model_name,
            cache_dir=str(Path(cache_dir).resolve()) if cache_dir else None,
            lazy_load=True,
        )

    def embed(self, text: str, *, document: bool = False) -> list[float]:
        method = self._model.passage_embed if document else self._model.query_embed
        return list(next(iter(method([text]))).tolist())

    def embed_many(
        self,
        texts: list[str],
        *,
        document: bool = False,
    ) -> list[list[float]]:
        method = self._model.passage_embed if document else self._model.query_embed
        return [vector.tolist() for vector in method(texts)]


def build_embedding(settings):
    provider = settings.rag_embedding_provider.strip().lower()
    if provider == "fastembed":
        return FastEmbedChineseEmbedding(
            settings.rag_embedding_model,
            cache_dir=settings.rag_embedding_cache_dir,
            expected_dimension=settings.rag_embedding_dimension,
        )
    if provider == "hash":
        return LocalChineseEmbedding(settings.rag_embedding_dimension)
    raise ValueError(f"unsupported RAG embedding provider: {provider}")


class BM25Index:
    def __init__(self, documents: list[list[str]], *, k1: float = 1.5, b: float = 0.75):
        self.documents = documents
        self.k1 = k1
        self.b = b
        self.document_lengths = [len(document) for document in documents]
        self.average_length = (
            sum(self.document_lengths) / len(self.document_lengths)
            if self.document_lengths
            else 0.0
        )
        self.term_frequencies = [Counter(document) for document in documents]
        document_frequency: Counter[str] = Counter()
        for document in documents:
            document_frequency.update(set(document))
        total = len(documents)
        self.idf = {
            term: math.log(1.0 + (total - frequency + 0.5) / (frequency + 0.5))
            for term, frequency in document_frequency.items()
        }

    def scores(self, query_tokens: list[str]) -> list[float]:
        if not self.documents:
            return []
        result: list[float] = []
        for frequencies, length in zip(self.term_frequencies, self.document_lengths):
            score = 0.0
            for term in set(query_tokens):
                frequency = frequencies.get(term, 0)
                if not frequency:
                    continue
                denominator = frequency + self.k1 * (
                    1.0
                    - self.b
                    + self.b * length / max(self.average_length, 1.0)
                )
                score += self.idf.get(term, 0.0) * (
                    frequency * (self.k1 + 1.0) / denominator
                )
            result.append(score)
        return result


def _document_title(text: str, fallback: str) -> str:
    match = HEADING.search(text)
    return match.group(2).strip() if match else fallback


def _section_at(headings: list[tuple[int, str]], position: int, title: str) -> str:
    section = title
    for start, value in headings:
        if start > position:
            break
        section = value
    return section


def _chunk_end(text: str, start: int, chunk_size: int) -> int:
    end = min(len(text), start + chunk_size)
    if end >= len(text):
        return len(text)
    lower_bound = start + int(chunk_size * 0.6)
    candidates = [
        text.rfind(boundary, lower_bound, end)
        for boundary in ("\n\n", "。", "！", "？", "；", "\n")
    ]
    boundary = max(candidates)
    if boundary >= lower_bound:
        return boundary + (2 if text.startswith("\n\n", boundary) else 1)
    return end


def chunk_markdown_files(
    knowledge_dir: Path,
    *,
    chunk_size: int = 512,
    overlap: int = 64,
) -> list[KnowledgeChunk]:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap must be non-negative and smaller than chunk_size")

    chunks: list[KnowledgeChunk] = []
    for path in sorted(knowledge_dir.glob("*.md"), key=lambda item: item.name):
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            continue
        title = _document_title(text, path.stem)
        headings = [(match.start(), match.group(2).strip()) for match in HEADING.finditer(text)]
        start = 0
        index = 0
        while start < len(text):
            end = _chunk_end(text, start, chunk_size)
            content = text[start:end].strip()
            if content:
                identity = f"{path.name}:{index}:{content}".encode("utf-8")
                chunk_id = hashlib.sha256(identity).hexdigest()[:32]
                chunks.append(
                    KnowledgeChunk(
                        chunk_id=chunk_id,
                        source=path.name,
                        title=title,
                        section=_section_at(headings, start, title),
                        index=index,
                        content=content,
                        char_start=start,
                        char_end=end,
                    )
                )
                index += 1
            if end >= len(text):
                break
            start = max(start + 1, end - overlap)
    return chunks
