from __future__ import annotations

import json
import time
from typing import Any

from sqlalchemy import select

from app.core.database import SessionLocal
from app.indexing.embeddings import EmbeddingBackendError
from app.indexing.language_search import search_language_traces
from app.indexing.qwen_query_backend import QwenPersistentQueryEmbeddingBackend
from app.indexing.retrieval_fusion import fuse_temporal_retrieval, temporal_gap_ms
from app.indexing.visual_search import search_visual_embeddings
from app.library_models import Embedding, Trace


EVAL_QUERIES = (
    ("sanding-axes", "sanding axes"),
    ("fractal-burning-setup", "fractal burning setup"),
    ("finished-staffs", "finished staffs"),
    ("epoxy-pour", "mixing and pouring epoxy"),
    ("gluing-sign", "gluing letters onto a sign"),
)
TOP_K = 5
CANDIDATE_K = 25


def _language_payload(result) -> dict[str, Any]:
    return {
        "traceCount": result.trace_count,
        "candidateCount": len(result.matches),
        "databaseMs": round(result.database_ms, 4),
        "scoringMs": round(result.scoring_ms, 4),
        "matches": [
            {
                "rank": rank,
                "traceId": match.trace_id,
                "mediaId": match.media_id,
                "startMs": match.start_ms,
                "endMs": match.end_ms,
                "score": round(match.score, 8),
                "text": match.text[:240],
            }
            for rank, match in enumerate(result.matches[:TOP_K], start=1)
        ],
    }


def _visual_payload(result) -> dict[str, Any]:
    return {
        "vectorCount": result.vector_count,
        "candidateCount": len(result.matches),
        "databaseMs": round(result.database_ms, 4),
        "scoringMs": round(result.scoring_ms, 4),
        "matches": [
            {
                "rank": rank,
                "traceId": match.trace_id,
                "mediaId": match.media_id,
                "startMs": match.start_ms,
                "endMs": match.end_ms,
                "score": round(match.score, 8),
            }
            for rank, match in enumerate(result.matches[:TOP_K], start=1)
        ],
    }


def _fusion_payload(matches) -> dict[str, Any]:
    return {
        "returned": len(matches),
        "matches": [
            {
                **match.as_dict(),
                "language": {
                    **match.as_dict()["language"],
                    "text": match.language_text[:240],
                },
            }
            for match in matches
        ],
        "policy": {
            "candidatePoolK": CANDIDATE_K,
            "sameMediaOnly": True,
            "rawScoreScalesMixed": False,
            "metadataUsed": False,
        },
    }


def _corpus_overlap_payload(db, *, model_id: str, dimension: int) -> dict[str, Any]:
    language_media_ids = set(
        db.scalars(
            select(Trace.media_id).where(Trace.trace_type == "language").distinct()
        ).all()
    )
    visual_media_ids = set(
        db.scalars(
            select(Trace.media_id)
            .join(Embedding, Embedding.trace_id == Trace.trace_id)
            .where(
                Trace.trace_type == "visual",
                Embedding.model_id == model_id,
                Embedding.embedding_dimension == dimension,
                Embedding.normalized.is_(True),
            )
            .distinct()
        ).all()
    )
    shared_media_ids = sorted(language_media_ids.intersection(visual_media_ids))
    return {
        "languageMediaCount": len(language_media_ids),
        "visualMediaCount": len(visual_media_ids),
        "sharedMediaCount": len(shared_media_ids),
        "sharedMediaIds": shared_media_ids,
    }


def _candidate_overlap_payload(language_matches, visual_matches) -> dict[str, Any]:
    language_media_ids = {match.media_id for match in language_matches}
    visual_media_ids = {match.media_id for match in visual_matches}
    shared_media_ids = sorted(language_media_ids.intersection(visual_media_ids))
    gaps = [
        temporal_gap_ms(
            language.start_ms,
            language.end_ms,
            visual.start_ms,
            visual.end_ms,
        )
        for language in language_matches
        for visual in visual_matches
        if language.media_id == visual.media_id
    ]
    return {
        "languageCandidateMediaCount": len(language_media_ids),
        "visualCandidateMediaCount": len(visual_media_ids),
        "sharedCandidateMediaCount": len(shared_media_ids),
        "sharedCandidateMediaIds": shared_media_ids,
        "nearestSameMediaGapMs": min(gaps) if gaps else None,
    }


def main() -> int:
    started = time.perf_counter()
    try:
        backend = QwenPersistentQueryEmbeddingBackend()
        query_started = time.perf_counter()
        query_vectors = backend.embed_text([text for _, text in EVAL_QUERIES])
        query_embedding_ms = (time.perf_counter() - query_started) * 1000.0
        if len(query_vectors) != len(EVAL_QUERIES):
            raise EmbeddingBackendError(
                f"Qwen returned {len(query_vectors)} query vectors for {len(EVAL_QUERIES)} evaluation queries"
            )

        rows: list[dict[str, Any]] = []
        with SessionLocal() as db:
            corpus_overlap = _corpus_overlap_payload(
                db,
                model_id=backend.model_id,
                dimension=backend.dimension,
            )
            for (query_id, text), query_vector in zip(EVAL_QUERIES, query_vectors, strict=True):
                language = search_language_traces(db, query=text, top_k=CANDIDATE_K)
                visual = search_visual_embeddings(
                    db,
                    query_vector=query_vector,
                    model_id=backend.model_id,
                    dimension=backend.dimension,
                    top_k=CANDIDATE_K,
                )
                fused = fuse_temporal_retrieval(
                    language.matches,
                    visual.matches,
                    top_k=TOP_K,
                )
                rows.append(
                    {
                        "queryId": query_id,
                        "query": text,
                        "language": _language_payload(language),
                        "visual": _visual_payload(visual),
                        "fusion": _fusion_payload(fused),
                        "candidateOverlap": _candidate_overlap_payload(
                            language.matches,
                            visual.matches,
                        ),
                    }
                )

        payload = {
            "schemaVersion": 4,
            "summary": f"Full-stream retrieval baseline completed for {len(rows)} fixed queries",
            "queryCount": len(rows),
            "topK": TOP_K,
            "candidatePoolK": CANDIDATE_K,
            "queryEmbeddingMs": round(query_embedding_ms, 4),
            "elapsedMs": round((time.perf_counter() - started) * 1000.0, 4),
            "modelId": backend.model_id,
            "queryRuntime": "persistent-isolated-worker",
            "corpusOverlap": corpus_overlap,
            "scoringIsolation": {
                "languageUsesTraceTextOnly": True,
                "visualUsesPersistedEmbeddingsOnly": True,
                "fusionUsesRanksAndTemporalProximityOnly": True,
                "filenameUsed": False,
                "titleUsed": False,
                "sourcePathUsed": False,
            },
            "queries": rows,
        }
        print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {
                    "summary": f"Full-stream retrieval baseline failed: {type(exc).__name__}: {exc}",
                    "elapsedMs": round((time.perf_counter() - started) * 1000.0, 4),
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
