from __future__ import annotations

import math
import threading
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from pathlib import Path
from typing import Iterable, Protocol


class Reranker(Protocol):
    def score(self, query: str, documents: list[str], *, timeout: float) -> list[float]: ...

    def status(self) -> dict[str, str | bool | None]: ...


def _sigmoid(value: float) -> float:
    if value >= 0:
        factor = math.exp(-value)
        return 1.0 / (1.0 + factor)
    factor = math.exp(value)
    return factor / (1.0 + factor)


class FastEmbedCrossEncoderReranker:
    """Lazy local Cross-Encoder with a non-blocking timeout fallback contract."""

    def __init__(
        self,
        model_name: str,
        *,
        cache_dir: str | Path | None = None,
        batch_size: int = 12,
    ):
        try:
            from fastembed.rerank.cross_encoder import TextCrossEncoder
        except ImportError as exc:  # pragma: no cover - dependency contract
            raise RuntimeError(
                "fastembed is required for the configured RAG reranker"
            ) from exc
        self.model_name = model_name
        self.batch_size = max(1, batch_size)
        self._model_factory = TextCrossEncoder
        self._cache_dir = str(Path(cache_dir).resolve()) if cache_dir else None
        self._model = None
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="mindnook-reranker",
        )
        self._lock = threading.Lock()
        self._future: Future[list[float]] | None = None
        self._state = "not_loaded"
        self._last_error: str | None = None

    def _run(self, query: str, documents: list[str]) -> list[float]:
        if self._model is None:
            self._model = self._model_factory(
                model_name=self.model_name,
                cache_dir=self._cache_dir,
                lazy_load=True,
            )
        raw_scores: Iterable[float] = self._model.rerank(
            query,
            documents,
            batch_size=self.batch_size,
        )
        scores = [_sigmoid(float(value)) for value in raw_scores]
        if len(scores) != len(documents):
            raise RuntimeError(
                f"reranker score count mismatch: {len(scores)} != {len(documents)}"
            )
        return scores

    def score(self, query: str, documents: list[str], *, timeout: float) -> list[float]:
        if not documents:
            return []
        with self._lock:
            if self._future is not None and not self._future.done():
                raise TimeoutError("reranker is still warming up or processing")
            self._state = "loading" if self._state == "not_loaded" else "running"
            self._future = self._executor.submit(self._run, query, documents)
            future = self._future
        try:
            scores = future.result(timeout=max(0.01, timeout))
        except TimeoutError:
            with self._lock:
                self._state = "warming_up"
                self._last_error = "reranker timeout; hybrid fallback used"
            raise
        except Exception as exc:
            with self._lock:
                self._state = "degraded"
                self._last_error = f"{type(exc).__name__}: {exc}"
            raise
        with self._lock:
            self._state = "ready"
            self._last_error = None
        return scores

    def status(self) -> dict[str, str | bool | None]:
        with self._lock:
            if self._future is not None and self._future.done() and self._state == "warming_up":
                try:
                    self._future.result()
                except Exception as exc:
                    self._state = "degraded"
                    self._last_error = f"{type(exc).__name__}: {exc}"
                else:
                    self._state = "ready"
                    self._last_error = None
            return {
                "enabled": True,
                "provider": "fastembed",
                "model": self.model_name,
                "state": self._state,
                "last_error": self._last_error,
            }


def build_reranker(settings) -> Reranker | None:
    # Test/Harness environments must stay deterministic and must never download
    # or execute the production Cross-Encoder implicitly. Component tests inject
    # an explicit fake reranker through HybridRAGService instead.
    if settings.app_env == "test" or not settings.rag_reranker_enabled:
        return None
    provider = settings.rag_reranker_provider.strip().lower()
    if provider == "fastembed":
        return FastEmbedCrossEncoderReranker(
            settings.rag_reranker_model,
            cache_dir=settings.rag_reranker_cache_dir,
            batch_size=settings.rag_reranker_batch_size,
        )
    raise ValueError(f"unsupported RAG reranker provider: {provider}")
