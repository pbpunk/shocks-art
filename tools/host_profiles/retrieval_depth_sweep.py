from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

LIVE_ROOT = Path(os.getenv("SHOCKS_HOST_LIVE_ROOT", Path(__file__).resolve().parents[2])).resolve()
os.chdir(LIVE_ROOT)
if str(LIVE_ROOT) not in sys.path:
    sys.path.insert(0, str(LIVE_ROOT))

from app.core.database import SessionLocal
from app.indexing.language_search import search_language_traces
from app.indexing.qwen_query_backend import QwenPersistentQueryEmbeddingBackend
from app.indexing.retrieval_fusion import fuse_temporal_retrieval
from app.indexing.visual_search import search_visual_embeddings


QUERIES: tuple[tuple[str, str, str | None], ...] = (
    ("sanding-axes", "sanding axes", "media_66612c0710ad4b8ba78e3653256af2fe"),
    ("fractal-burning-setup", "fractal burning setup", "media_4a2b9b61b1cd44e7bd820ed68dbf207d"),
    ("finished-staffs", "finished staffs", "media_0a571dc5e48942fc9b9d98e27609eeb0"),
    ("epoxy-pour", "mixing and pouring epoxy", None),
    ("gluing-sign", "gluing letters onto a sign", "media_53c498d982c14ec680bacf2be2f4dfa0"),
)
DEPTHS = (25, 50, 100, 500)
MAX_DEPTH = max(DEPTHS)
TOP_K = 5


def emit(payload: dict[str, Any], code: int = 0) -> int:
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    return code


def _match_payload(match) -> dict[str, Any]:
    return {
        "mediaId": match.media_id,
        "startMs": match.start_ms,
        "endMs": match.end_ms,
        "gapMs": match.gap_ms,
        "score": round(match.score, 8),
        "languageRank": match.language_rank,
        "visualRank": match.visual_rank,
        "languageText": match.language_text[:240],
    }


def main() -> int:
    started = time.perf_counter()
    try:
        backend = QwenPersistentQueryEmbeddingBackend()
        vectors = backend.embed_text([query for _, query, _ in QUERIES])
        if len(vectors) != len(QUERIES):
            raise RuntimeError(f"Qwen returned {len(vectors)} vectors for {len(QUERIES)} fixed queries")

        rows: list[dict[str, Any]] = []
        with SessionLocal() as db:
            for (query_id, query, expected_media_id), vector in zip(QUERIES, vectors, strict=True):
                language = search_language_traces(db, query=query, top_k=MAX_DEPTH)
                visual = search_visual_embeddings(
                    db,
                    query_vector=vector,
                    model_id=backend.model_id,
                    dimension=backend.dimension,
                    top_k=5000,
                )
                depth_rows: list[dict[str, Any]] = []
                for depth in DEPTHS:
                    language_candidates = language.matches[:depth]
                    visual_candidates = visual.matches[:depth]
                    fused = fuse_temporal_retrieval(
                        language_candidates,
                        visual_candidates,
                        top_k=TOP_K,
                    )
                    target_fused = []
                    target_language_count = None
                    target_visual_count = None
                    if expected_media_id is not None:
                        target_language = [match for match in language_candidates if match.media_id == expected_media_id]
                        target_visual = [match for match in visual_candidates if match.media_id == expected_media_id]
                        target_language_count = len(target_language)
                        target_visual_count = len(target_visual)
                        target_fused = fuse_temporal_retrieval(target_language, target_visual, top_k=TOP_K)
                    depth_rows.append(
                        {
                            "candidateDepth": depth,
                            "languageCandidates": len(language_candidates),
                            "visualCandidates": len(visual_candidates),
                            "globalFusedCount": len(fused),
                            "globalFused": [_match_payload(match) for match in fused],
                            "expectedMediaId": expected_media_id,
                            "targetLanguageCandidates": target_language_count,
                            "targetVisualCandidates": target_visual_count,
                            "targetFusedCount": len(target_fused) if expected_media_id is not None else None,
                            "targetFused": [_match_payload(match) for match in target_fused],
                        }
                    )
                rows.append(
                    {
                        "queryId": query_id,
                        "query": query,
                        "deepLanguageReturned": len(language.matches),
                        "deepVisualReturned": len(visual.matches),
                        "depths": depth_rows,
                    }
                )

        return emit(
            {
                "summary": "Fixed retrieval candidate-depth sweep completed",
                "candidateDepths": list(DEPTHS),
                "topK": TOP_K,
                "queryCount": len(rows),
                "queries": rows,
                "modelId": backend.model_id,
                "dimension": backend.dimension,
                "temporalRuleChanged": False,
                "metadataUsedForSelectionOrScoring": False,
                "stateMutationRequested": False,
                "durationSeconds": round(time.perf_counter() - started, 3),
            }
        )
    except Exception as exc:
        return emit(
            {
                "summary": f"Fixed retrieval candidate-depth sweep failed: {type(exc).__name__}: {exc}",
                "durationSeconds": round(time.perf_counter() - started, 3),
            },
            1,
        )


if __name__ == "__main__":
    raise SystemExit(main())
