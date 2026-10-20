from __future__ import annotations

import hashlib
import threading
import uuid
from pathlib import Path


class MemoryIndex:
    """Independent dynamic collection; never invokes knowledge corpus cleanup."""

    def __init__(self, settings, *, embedding=None, client=None):
        self.settings = settings
        self.embedding = embedding
        self.client = client
        self.collection = None
        self._lock = threading.RLock()
        self._test_namespace = uuid.uuid4().hex[:10]

    def _ensure(self):
        with self._lock:
            if self.collection is not None:
                return
            import chromadb
            from chromadb.config import Settings as ChromaSettings
            from app.rag.text import build_embedding
            if self.embedding is None:
                settings = self.settings
                if settings.app_env == "test":
                    settings = settings.model_copy(update={"rag_embedding_provider": "hash"})
                self.embedding = build_embedding(settings)
            if self.client is None:
                options = ChromaSettings(anonymized_telemetry=False)
                self.client = (chromadb.EphemeralClient(settings=options) if self.settings.app_env == "test"
                               else chromadb.PersistentClient(path=str(Path(self.settings.rag_chroma_path).resolve()), settings=options))
            signature = hashlib.sha256(
                f"{type(self.embedding).__name__}:{self.settings.rag_embedding_model}:{self.embedding.dimension}".encode()
            ).hexdigest()[:10]
            name = f"{self.settings.memory_collection_name}_{signature}"
            if self.settings.app_env == "test":
                name += f"_{self._test_namespace}"
            self.collection = self.client.get_or_create_collection(name=name, metadata={"hnsw:space": "cosine"})

    def upsert(self, *, memory_id, student_id, content, version):
        self._ensure()
        with self._lock:
            self.collection.upsert(ids=[memory_id], documents=[content],
                                   embeddings=[self.embedding.embed(content, document=True)],
                                   metadatas=[{"student_id": str(student_id), "version": version}])

    def delete(self, memory_id):
        self._ensure()
        with self._lock:
            self.collection.delete(ids=[memory_id])

    def search(self, student_id, query, count):
        self._ensure()
        with self._lock:
            result = self.collection.query(query_embeddings=[self.embedding.embed(query)], n_results=count,
                                           where={"student_id": str(student_id)}, include=["distances", "metadatas"])
        return [(key, max(0.0, 1.0 - distance), metadata.get("version", 0))
                for key, distance, metadata in zip(result["ids"][0], result["distances"][0], result["metadatas"][0])]

    def similarity(self, query, content):
        self._ensure()
        a, b = self.embedding.embed(query), self.embedding.embed(content, document=True)
        import math
        return sum(x * y for x, y in zip(a, b)) / max(1e-12, math.sqrt(sum(x*x for x in a) * sum(y*y for y in b)))

    @property
    def signature(self):
        self._ensure()
        return self.collection.name
