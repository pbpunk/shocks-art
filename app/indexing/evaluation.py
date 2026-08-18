from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.indexing.embedding_service import float32_blob_to_vector, normalize_vector
from app.indexing.embeddings import EmbeddingBackend, EmbeddingBackendError
from app.library_models import Embedding, Trace


@dataclass(frozen=True)
class EvaluationQuery:
    query_id: str
    kind: str
    text: str
    note: str = ""


@dataclass(frozen=True)
class EvaluationSpec:
    dimensions: tuple[int, ...]
    top_k: int
    queries: tuple[EvaluationQuery, ...]
    description: str = ""


@dataclass(frozen=True)
class EvaluationCandidate:
    trace_id: str
    media_id: str
    start_ms: int
    end_ms: int
    artifact_path: str
    vector: tuple[float, ...]


def load_evaluation_spec(path: Path) -> EvaluationSpec:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read visual evaluation spec {path}: {exc}") from exc

    if payload.get("schemaVersion") != 1:
        raise ValueError("visual evaluation spec schemaVersion must be 1")

    dimensions = tuple(int(value) for value in payload.get("dimensions", []))
    if not dimensions or any(value <= 0 for value in dimensions):
        raise ValueError("visual evaluation dimensions must contain positive integers")
    if len(set(dimensions)) != len(dimensions):
        raise ValueError("visual evaluation dimensions must be unique")

    top_k = int(payload.get("topK", 0))
    if top_k <= 0:
        raise ValueError("visual evaluation topK must be greater than zero")

    queries: list[EvaluationQuery] = []
    seen_ids: set[str] = set()
    for item in payload.get("queries", []):
        query_id = str(item.get("id", "")).strip()
        kind = str(item.get("kind", "")).strip().lower()
        text = str(item.get("text", "")).strip()
        if not query_id or query_id in seen_ids:
            raise ValueError("visual evaluation query IDs must be non-empty and unique")
        if kind not in {"positive", "control"}:
            raise ValueError(f"visual evaluation query {query_id} kind must be positive or control")
        if not text:
            raise ValueError(f"visual evaluation query {query_id} text must not be empty")
        seen_ids.add(query_id)
        queries.append(
            EvaluationQuery(
                query_id=query_id,
                kind=kind,
                text=text,
                note=str(item.get("note", "")),
            )
        )
    if not queries:
        raise ValueError("visual evaluation spec must contain at least one query")

    return EvaluationSpec(
        dimensions=dimensions,
        top_k=top_k,
        queries=tuple(queries),
        description=str(payload.get("description", "")),
    )


def _load_candidates(
    db: Session,
    *,
    model_id: str,
    native_dimension: int,
) -> tuple[list[EvaluationCandidate], float]:
    started = time.perf_counter()
    rows = list(
        db.execute(
            select(Embedding, Trace)
            .join(Trace, Embedding.trace_id == Trace.trace_id)
            .where(
                Trace.trace_type == "visual",
                Embedding.model_id == model_id,
                Embedding.embedding_dimension == native_dimension,
                Embedding.normalized.is_(True),
            )
            .order_by(Embedding.trace_id.asc())
        ).all()
    )

    expected_blob_size = native_dimension * 4
    candidates: list[EvaluationCandidate] = []
    for embedding, trace in rows:
        if len(embedding.vector_blob) != expected_blob_size:
            raise EmbeddingBackendError(
                f"Embedding {embedding.embedding_id} has {len(embedding.vector_blob)} bytes; "
                f"expected {expected_blob_size} for float32 dimension {native_dimension}"
            )
        vector = float32_blob_to_vector(embedding.vector_blob)
        if len(vector) != native_dimension:
            raise EmbeddingBackendError(
                f"Embedding {embedding.embedding_id} decoded to dimension {len(vector)}; "
                f"expected {native_dimension}"
            )
        candidates.append(
            EvaluationCandidate(
                trace_id=trace.trace_id,
                media_id=trace.media_id,
                start_ms=trace.start_ms,
                end_ms=trace.end_ms,
                artifact_path=trace.artifact_path,
                vector=tuple(vector),
            )
        )
    return candidates, (time.perf_counter() - started) * 1000.0


def _truncate_normalize(vector: Sequence[float], dimension: int) -> list[float]:
    if dimension > len(vector):
        raise EmbeddingBackendError(
            f"requested evaluation dimension {dimension} exceeds available dimension {len(vector)}"
        )
    return normalize_vector(vector[:dimension])


def _score(query: Sequence[float], candidate: Sequence[float]) -> float:
    if len(query) != len(candidate):
        raise EmbeddingBackendError(
            f"evaluation dimension mismatch: query={len(query)}, candidate={len(candidate)}"
        )
    value = sum(float(left) * float(right) for left, right in zip(query, candidate, strict=True))
    return max(-1.0, min(1.0, value))


def evaluate_visual_search(
    db: Session,
    *,
    backend: EmbeddingBackend,
    spec: EvaluationSpec,
) -> dict[str, Any]:
    """Run a blind multi-dimension semantic retrieval evaluation.

    All query text is embedded in one backend call. Candidate rankings use only
    persisted visual vectors from the exact backend generation. Media titles,
    filenames, paths, and source metadata are intentionally absent from this
    bundle so neither scoring nor human review can infer relevance from names.
    """

    if any(dimension > backend.dimension for dimension in spec.dimensions):
        raise ValueError(
            f"evaluation dimensions {spec.dimensions} exceed backend native dimension {backend.dimension}"
        )

    candidates, candidate_load_ms = _load_candidates(
        db,
        model_id=backend.model_id,
        native_dimension=backend.dimension,
    )
    if not candidates:
        raise EmbeddingBackendError(
            f"no normalized visual embeddings found for generation {backend.model_id}"
        )

    embedding_started = time.perf_counter()
    query_vectors = backend.embed_text([query.text for query in spec.queries])
    query_embedding_ms = (time.perf_counter() - embedding_started) * 1000.0
    if len(query_vectors) != len(spec.queries):
        raise EmbeddingBackendError(
            f"backend returned {len(query_vectors)} query vectors for {len(spec.queries)} evaluation queries"
        )

    dimension_payloads: list[dict[str, Any]] = []
    scoring_started = time.perf_counter()
    for dimension in spec.dimensions:
        candidate_vectors = {
            candidate.trace_id: _truncate_normalize(candidate.vector, dimension)
            for candidate in candidates
        }
        query_payloads: list[dict[str, Any]] = []
        for query, native_query_vector in zip(spec.queries, query_vectors, strict=True):
            query_vector = _truncate_normalize(native_query_vector, dimension)
            ranked = sorted(
                (
                    (
                        _score(query_vector, candidate_vectors[candidate.trace_id]),
                        candidate,
                    )
                    for candidate in candidates
                ),
                key=lambda item: (-item[0], item[1].trace_id),
            )[: spec.top_k]
            query_payloads.append(
                {
                    "queryId": query.query_id,
                    "kind": query.kind,
                    "text": query.text,
                    "note": query.note,
                    "matches": [
                        {
                            "rank": rank,
                            "traceId": candidate.trace_id,
                            "mediaId": candidate.media_id,
                            "startMs": candidate.start_ms,
                            "endMs": candidate.end_ms,
                            "artifactPath": candidate.artifact_path,
                            "score": round(score, 8),
                        }
                        for rank, (score, candidate) in enumerate(ranked, start=1)
                    ],
                }
            )
        dimension_payloads.append(
            {
                "dimension": dimension,
                "queries": query_payloads,
            }
        )
    scoring_ms = (time.perf_counter() - scoring_started) * 1000.0

    return {
        "schemaVersion": 1,
        "modelId": backend.model_id,
        "nativeDimension": backend.dimension,
        "vectorCount": len(candidates),
        "topK": spec.top_k,
        "dimensions": list(spec.dimensions),
        "queryCount": len(spec.queries),
        "positiveQueryCount": sum(1 for query in spec.queries if query.kind == "positive"),
        "controlQueryCount": sum(1 for query in spec.queries if query.kind == "control"),
        "queryEmbeddingMs": round(query_embedding_ms, 4),
        "candidateLoadMs": round(candidate_load_ms, 4),
        "scoringMs": round(scoring_ms, 4),
        "scoringIsolation": {
            "usesVisualEmbeddingsOnly": True,
            "filenameUsed": False,
            "titleUsed": False,
            "sourcePathUsed": False,
            "presentationMetadataIncluded": False,
        },
        "description": spec.description,
        "results": dimension_payloads,
    }
