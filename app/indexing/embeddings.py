from __future__ import annotations

import math
from pathlib import Path
from threading import Lock
from typing import Callable, Protocol, Sequence, runtime_checkable


Vector = list[float]


class EmbeddingBackendError(RuntimeError):
    pass


@runtime_checkable
class EmbeddingBackend(Protocol):
    """Minimal multimodal embedding contract used by the offline indexer.

    Implementations must expose stable model identity and output dimension. The
    web application imports this protocol safely; heavyweight ML libraries belong
    behind a lazy loader and are never imported as a side effect of FastAPI
    startup.
    """

    model_id: str
    dimension: int

    def embed_text(self, texts: Sequence[str]) -> list[Vector]: ...

    def embed_images(self, image_paths: Sequence[Path]) -> list[Vector]: ...


BackendLoader = Callable[[], EmbeddingBackend]


class LazyEmbeddingBackend:
    """Load a heavyweight embedding implementation only on first inference."""

    def __init__(self, *, model_id: str, dimension: int, loader: BackendLoader):
        if not model_id.strip():
            raise ValueError("model_id must not be empty")
        if dimension <= 0:
            raise ValueError("dimension must be greater than zero")
        self.model_id = model_id
        self.dimension = dimension
        self._loader = loader
        self._backend: EmbeddingBackend | None = None
        self._load_lock = Lock()

    @property
    def is_loaded(self) -> bool:
        return self._backend is not None

    def _get_backend(self) -> EmbeddingBackend:
        if self._backend is None:
            with self._load_lock:
                if self._backend is None:
                    backend = self._loader()
                    if backend.model_id != self.model_id:
                        raise EmbeddingBackendError(
                            f"loaded backend model_id {backend.model_id!r} does not match declared {self.model_id!r}"
                        )
                    if backend.dimension != self.dimension:
                        raise EmbeddingBackendError(
                            f"loaded backend dimension {backend.dimension} does not match declared {self.dimension}"
                        )
                    self._backend = backend
        return self._backend

    def embed_text(self, texts: Sequence[str]) -> list[Vector]:
        values = list(texts)
        if not values:
            return []
        vectors = self._get_backend().embed_text(values)
        return self._validate_vectors(vectors, expected_count=len(values))

    def embed_images(self, image_paths: Sequence[Path]) -> list[Vector]:
        values = [Path(path) for path in image_paths]
        if not values:
            return []
        vectors = self._get_backend().embed_images(values)
        return self._validate_vectors(vectors, expected_count=len(values))

    def _validate_vectors(self, vectors: Sequence[Sequence[float]], *, expected_count: int) -> list[Vector]:
        materialized = [list(vector) for vector in vectors]
        if len(materialized) != expected_count:
            raise EmbeddingBackendError(
                f"backend returned {len(materialized)} vectors for {expected_count} inputs"
            )
        for index, vector in enumerate(materialized):
            if len(vector) != self.dimension:
                raise EmbeddingBackendError(
                    f"vector {index} has dimension {len(vector)}; expected {self.dimension}"
                )
            if any(not math.isfinite(float(value)) for value in vector):
                raise EmbeddingBackendError(f"vector {index} contains a non-finite value")
        return materialized
