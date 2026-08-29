from __future__ import annotations

import json
import time
from typing import Any

from app.core.database import SessionLocal
from app.indexing.embeddings import EmbeddingBackendError
from app.indexing.language_search import search_language_traces
from app.indexing.qwen_query_backend import QwenPersistentQueryEmbeddingBackend
from app.indexing.retrieval_fusion import fuse_temporal_retrieval
from app.indexing.visual_search import search_visual_embeddings


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
                    }
                )

        payload = {
            "schemaVersion": 3,
            "summary": f"Full-stream retrieval baseline completed for {len(rows)} fixed queries",
            "queryCount": len(rows),
            "topK": TOP_K,
            "candidatePoolK": CANDIDATE_K,
            "queryEmbeddingMs": round(query_embedding_ms, 4),
            "elapsedMs": round((time.perf_counter() - started) * 1000.0, 4),
            "modelId": backend.model_id,
            "queryRuntime": "persistent-isolated-worker",
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
