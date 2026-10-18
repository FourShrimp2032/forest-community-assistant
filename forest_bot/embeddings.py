"""Local multilingual sentence embeddings (fastembed / ONNX). Optional: keyword search works without it."""

from __future__ import annotations

import logging

import numpy as np

log = logging.getLogger(__name__)


class Embedder:
    def __init__(self, model_name: str, cache_dir: str | None = None, enabled: bool = True):
        self.model_name = model_name
        self.cache_dir = cache_dir
        self.enabled = enabled
        self._model = None
        self._failed = False

    @property
    def available(self) -> bool:
        if not self.enabled or self._failed:
            return False
        if self._model is None:
            try:
                from fastembed import TextEmbedding
                self._model = TextEmbedding(self.model_name, cache_dir=self.cache_dir)
                log.info("Embedding model loaded: %s", self.model_name)
            except Exception as exc:
                log.warning("Embedding model %s unavailable, using keyword search only: %s", self.model_name, exc)
                self._failed = True
                return False
        return True

    def _embed(self, texts: list[str]) -> np.ndarray:
        vectors = np.array(list(self._model.embed(texts)), dtype=np.float32)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return vectors / norms

    def embed_passages(self, texts: list[str]) -> list[bytes]:
        return [v.tobytes() for v in self._embed(texts)]

    def embed_query(self, text: str) -> np.ndarray:
        return self._embed([text])[0]


def from_bytes(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)
