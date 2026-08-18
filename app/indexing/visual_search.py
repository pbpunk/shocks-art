from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.indexing.embedding_service import float32_blob_to_vector, normalize_vector
from app.indexing.embeddings import EmbeddingBackendError
from app.library_models import Embedding, Trace


@dataclass(frozen=True)
class VisualSearchMatch:
    trace_id: str
    media_id: str
    start_ms: int
    end_ms: int
    artifact_path: str
    score: float

    def as_dict(self) -> dict:
        return {
            "traceId": self.trace_id,
            "mediaId": self.media_id,
            "startMs": self.start_ms,
            "endMs": self.end_ms,
            "artifactPath": self.artifact_path,
            "score": round(self.score, 8),
        }


@dataclass(frozen=True)
class VisualSearchResult:
    model_id: str
    dimension: int
    vector_count: int
    top_k: int
    database_ms: float
    scoring_ms: float
    elapsed_ms: float
    matches: tuple[VisualSearchMatch, ...]

    def as_dict(self) -> dict:
        return {
            "modelId": self.model_id,
            "dimension": self.dimension,
            "vectorCount": self.vector_count,
            "topK": self.top_k,
            "returned": len(self.matches),
            "databaseMs": round(self.database_ms, 4),
            "scoringMs": round(self.scoring_ms, 4),
            "elapsedMs": round(self.elapsed_ms, 4),
            "matches": [match.as_dict() for match in self.matches],
        }


def _cosine_for_normalized(query: Sequence[float], candidate: Sequence[float]) -> float:
    if len(query) != len(candidate):
        raise EmbeddingBackendError(
            f"cosine dimension mismatch: query={len(query)}, candidate={len(candidate)}"
        )
    score = sum(float(left) * float(right) for left, right in zip(query, candidate, strict=True))
    # Float32 persistence can move a mathematically unit cosine a few ulps beyond
    # the canonical range. Clamp only that representation noise.
    return max(-1.0, min(1.0, score))


def search_visual_embeddings(
    db: Session,
    *,
    query_vector: Sequence[float],
    model_id: str,
    dimension: int,
    top_k: int = 10,
) -> VisualSearchResult:
    """Brute-force cosine search over one exact normalized embedding generation.

    The prototype intentionally reads normalized vectors from SQLite and scores
    them in-process. It never mixes model/config generations. Ranking is stable:
    score descending, then Trace ID ascending for exact ties.
    """

    if not model_id.strip():
        raise ValueError("model_id must not be empty")
    if dimension <= 0:
        raise ValueError("dimension must be greater than zero")
    if top_k <= 0:
        raise ValueError("top_k must be greater than zero")

    query = normalize_vector(query_vector)
    if len(query) != dimension:
        raise EmbeddingBackendError(
            f"query embedding has dimension {len(query)}; expected {dimension}"
        )

    started = time.perf_counter()
    database_started = time.perf_counter()
    rows = list(
        db.execute(
            select(Embedding, Trace)
            .join(Trace, Embedding.trace_id == Trace.trace_id)
            .where(
                Trace.trace_type == "visual",
                Embedding.model_id == model_id,
                Embedding.embedding_dimension == dimension,
                Embedding.normalized.is_(True),
            )
            .order_by(Embedding.trace_id.asc())
        ).all()
    )
    database_ms = (time.perf_counter() - database_started) * 1000.0

    scoring_started = time.perf_counter()
    matches: list[VisualSearchMatch] = []
    expected_blob_size = dimension * 4
    for embedding, trace in rows:
        if len(embedding.vector_blob) != expected_blob_size:
            raise EmbeddingBackendError(
                f"Embedding {embedding.embedding_id} has {len(embedding.vector_blob)} bytes; "
                f"expected {expected_blob_size} for float32 dimension {dimension}"
            )
        candidate = float32_blob_to_vector(embedding.vector_blob)
        if len(candidate) != dimension:
            raise EmbeddingBackendError(
                f"Embedding {embedding.embedding_id} decoded to dimension {len(candidate)}; expected {dimension}"
            )
        matches.append(
            VisualSearchMatch(
                trace_id=trace.trace_id,
                media_id=trace.media_id,
                start_ms=trace.start_ms,
                end_ms=trace.end_ms,
                artifact_path=trace.artifact_path,
                score=_cosine_for_normalized(query, candidate),
            )
        )

    matches.sort(key=lambda match: (-match.score, match.trace_id))
    scoring_ms = (time.perf_counter() - scoring_started) * 1000.0
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    return VisualSearchResult(
        model_id=model_id,
        dimension=dimension,
        vector_count=len(rows),
        top_k=top_k,
        database_ms=database_ms,
        scoring_ms=scoring_ms,
        elapsed_ms=elapsed_ms,
        matches=tuple(matches[:top_k]),
    )
