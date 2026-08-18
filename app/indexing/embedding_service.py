from __future__ import annotations

import math
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.indexing.embeddings import EmbeddingBackend, EmbeddingBackendError, Vector
from app.library_models import Embedding, Trace


@dataclass(frozen=True)
class VisualEmbeddingResult:
    model_id: str
    dimension: int
    considered: int
    created: int
    reused: int

    def as_dict(self) -> dict:
        return {
            "modelId": self.model_id,
            "dimension": self.dimension,
            "considered": self.considered,
            "created": self.created,
            "reused": self.reused,
        }


def normalize_vector(vector: Sequence[float]) -> Vector:
    values = [float(value) for value in vector]
    if not values:
        raise EmbeddingBackendError("embedding vector must not be empty")
    if any(not math.isfinite(value) for value in values):
        raise EmbeddingBackendError("embedding vector contains a non-finite value")
    norm = math.sqrt(sum(value * value for value in values))
    if not math.isfinite(norm) or norm <= 0:
        raise EmbeddingBackendError("embedding vector has an invalid norm")
    return [value / norm for value in values]


def vector_to_float32_blob(vector: Sequence[float]) -> bytes:
    return array("f", (float(value) for value in vector)).tobytes()


def float32_blob_to_vector(blob: bytes) -> Vector:
    values = array("f")
    values.frombytes(blob)
    return list(values)


def _safe_artifact_path(index_root: Path, artifact_path: str) -> Path:
    root = index_root.resolve()
    candidate = (root / artifact_path).resolve()
    if candidate != root and root not in candidate.parents:
        raise EmbeddingBackendError(f"Trace artifact escapes Library index root: {artifact_path}")
    if not candidate.is_file():
        raise EmbeddingBackendError(f"Trace artifact is missing: {artifact_path}")
    return candidate


def index_visual_trace_embeddings(
    db: Session,
    *,
    index_root: Path,
    backend: EmbeddingBackend,
    limit: int | None = None,
) -> VisualEmbeddingResult:
    """Generate one normalized embedding generation for visual Trace artifacts.

    Existing rows for the exact backend model generation and dimension are
    reused. A different model/config generation uses a different model_id and
    therefore creates a separate row rather than silently replacing vectors.
    """

    query = (
        select(Trace)
        .where(Trace.trace_type == "visual")
        .order_by(Trace.media_id.asc(), Trace.start_ms.asc(), Trace.trace_id.asc())
    )
    if limit is not None:
        if limit <= 0:
            raise ValueError("limit must be greater than zero when provided")
        query = query.limit(limit)

    traces = list(db.scalars(query).all())
    if not traces:
        return VisualEmbeddingResult(
            model_id=backend.model_id,
            dimension=backend.dimension,
            considered=0,
            created=0,
            reused=0,
        )

    trace_ids = [trace.trace_id for trace in traces]
    existing = list(
        db.scalars(
            select(Embedding).where(
                Embedding.trace_id.in_(trace_ids),
                Embedding.model_id == backend.model_id,
                Embedding.embedding_dimension == backend.dimension,
            )
        ).all()
    )
    existing_by_trace = {embedding.trace_id: embedding for embedding in existing}

    missing_traces = [trace for trace in traces if trace.trace_id not in existing_by_trace]
    artifact_paths = [_safe_artifact_path(index_root, trace.artifact_path) for trace in missing_traces]

    if artifact_paths:
        vectors = backend.embed_images(artifact_paths)
        if len(vectors) != len(missing_traces):
            raise EmbeddingBackendError(
                f"backend returned {len(vectors)} vectors for {len(missing_traces)} visual Traces"
            )
        for trace, vector in zip(missing_traces, vectors, strict=True):
            normalized = normalize_vector(vector)
            if len(normalized) != backend.dimension:
                raise EmbeddingBackendError(
                    f"Trace {trace.trace_id} embedding has dimension {len(normalized)}; expected {backend.dimension}"
                )
            db.add(
                Embedding(
                    trace_id=trace.trace_id,
                    model_id=backend.model_id,
                    embedding_dimension=backend.dimension,
                    dtype="float32",
                    vector_blob=vector_to_float32_blob(normalized),
                    normalized=True,
                )
            )
        db.commit()

    return VisualEmbeddingResult(
        model_id=backend.model_id,
        dimension=backend.dimension,
        considered=len(traces),
        created=len(missing_traces),
        reused=len(existing),
    )
