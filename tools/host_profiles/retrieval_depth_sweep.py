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


QUERIES: tuple[tuple[str, str, str | None, str | None], ...] = (
    ("sanding-axes", "sanding axes", "media_66612c0710ad4b8ba78e3653256af2fe", None),
    (
        "fractal-burning-setup",
        "fractal burning setup",
        "media_4a2b9b61b1cd44e7bd820ed68dbf207d",
        "trace_1a81f877bcba4a4aa04d647745424d14",
    ),
    (
        "finished-staffs",
        "finished staffs",
        "media_0a571dc5e48942fc9b9d98e27609eeb0",
        "trace_881afad9478944918286f370a9aa1721",
    ),
    ("epoxy-pour", "mixing and pouring epoxy", None, None),
    (
        "gluing-sign",
        "gluing letters onto a sign",
        "media_53c498d982c14ec680bacf2be2f4dfa0",
        "trace_15f39d71d81241cbb22519233b4e347e",
    ),
)
DEPTHS = (25, 50, 100, 500)
MAX_DEPTH = max(DEPTHS)
TOP_K = 5
MAX_RECEIPT_JSON_CHARS = 28_000
TEXT_SNIPPET_CHARS = 56


def emit(payload: dict[str, Any], code: int = 0) -> int:
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    if code == 0 and len(encoded) > MAX_RECEIPT_JSON_CHARS:
        print(
            json.dumps(
                {
                    "summary": "Fixed retrieval candidate-depth sweep receipt exceeded compact bridge budget",
                    "receiptJsonChars": len(encoded),
                    "receiptBudgetChars": MAX_RECEIPT_JSON_CHARS,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return 1
    print(encoded)
    return code


def _global_rank_map(matches) -> dict[str, int]:
    return {match.trace_id: rank for rank, match in enumerate(matches, start=1)}


def _compact_match(match, *, language_ranks: dict[str, int], visual_ranks: dict[str, int]) -> dict[str, Any]:
    return {
        "mediaId": match.media_id,
        "gapMs": match.gap_ms,
        "languageRank": language_ranks.get(match.language_trace_id),
        "visualRank": visual_ranks.get(match.visual_trace_id),
        "languageTraceId": match.language_trace_id,
        "visualTraceId": match.visual_trace_id,
        "languageText": match.language_text[:TEXT_SNIPPET_CHARS],
    }


def _first_match(matches, predicate):
    return next((match for match in matches if predicate(match)), None)


def main() -> int:
    started = time.perf_counter()
    try:
        backend = QwenPersistentQueryEmbeddingBackend()
        vectors = backend.embed_text([query for _, query, _, _ in QUERIES])
        if len(vectors) != len(QUERIES):
            raise RuntimeError(f"Qwen returned {len(vectors)} vectors for {len(QUERIES)} fixed queries")

        rows: list[dict[str, Any]] = []
        with SessionLocal() as db:
            for (query_id, query, expected_media_id, anchor_language_trace_id), vector in zip(
                QUERIES, vectors, strict=True
            ):
                language = search_language_traces(db, query=query, top_k=MAX_DEPTH)
                visual = search_visual_embeddings(
                    db,
                    query_vector=vector,
                    model_id=backend.model_id,
                    dimension=backend.dimension,
                    top_k=5000,
                )
                language_ranks = _global_rank_map(language.matches)
                visual_ranks = _global_rank_map(visual.matches)
                depth_rows: list[dict[str, Any]] = []

                for depth in DEPTHS:
                    language_candidates = language.matches[:depth]
                    visual_candidates = visual.matches[:depth]
                    fused = fuse_temporal_retrieval(
                        language_candidates,
                        visual_candidates,
                        top_k=TOP_K,
                    )
                    depth_payload: dict[str, Any] = {
                        "candidateDepth": depth,
                        "globalFusedCount": len(fused),
                        "globalMediaIds": [match.media_id for match in fused],
                    }

                    if expected_media_id is None:
                        depth_payload["globalTop"] = [
                            _compact_match(
                                match,
                                language_ranks=language_ranks,
                                visual_ranks=visual_ranks,
                            )
                            for match in fused
                        ]
                    else:
                        target_language = [
                            match for match in language_candidates if match.media_id == expected_media_id
                        ]
                        target_visual = [
                            match for match in visual_candidates if match.media_id == expected_media_id
                        ]
                        target_fused = fuse_temporal_retrieval(
                            target_language,
                            target_visual,
                            top_k=TOP_K,
                        )
                        expected_global = _first_match(
                            fused,
                            lambda match: match.media_id == expected_media_id,
                        )
                        depth_payload.update(
                            {
                                "expectedMediaInGlobalTop5": expected_global is not None,
                                "expectedGlobalBest": (
                                    _compact_match(
                                        expected_global,
                                        language_ranks=language_ranks,
                                        visual_ranks=visual_ranks,
                                    )
                                    if expected_global is not None
                                    else None
                                ),
                                "targetLanguageCandidates": len(target_language),
                                "targetVisualCandidates": len(target_visual),
                                "targetFusedCount": len(target_fused),
                            }
                        )

                        if anchor_language_trace_id is not None:
                            anchor_language = _first_match(
                                target_language,
                                lambda match: match.trace_id == anchor_language_trace_id,
                            )
                            anchor_fused = (
                                fuse_temporal_retrieval(
                                    [anchor_language],
                                    target_visual,
                                    top_k=TOP_K,
                                )
                                if anchor_language is not None and target_visual
                                else ()
                            )
                            anchor_global = _first_match(
                                fused,
                                lambda match: match.language_trace_id == anchor_language_trace_id,
                            )
                            depth_payload.update(
                                {
                                    "anchorLanguageTraceId": anchor_language_trace_id,
                                    "anchorLanguagePresent": anchor_language is not None,
                                    "anchorFusedCount": len(anchor_fused),
                                    "anchorBest": (
                                        _compact_match(
                                            anchor_fused[0],
                                            language_ranks=language_ranks,
                                            visual_ranks=visual_ranks,
                                        )
                                        if anchor_fused
                                        else None
                                    ),
                                    "anchorInGlobalTop5": anchor_global is not None,
                                    "anchorGlobal": (
                                        _compact_match(
                                            anchor_global,
                                            language_ranks=language_ranks,
                                            visual_ranks=visual_ranks,
                                        )
                                        if anchor_global is not None
                                        else None
                                    ),
                                }
                            )

                    depth_rows.append(depth_payload)

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
                "summary": "Fixed retrieval candidate-depth sweep completed with decision-only receipt",
                "candidateDepths": list(DEPTHS),
                "topK": TOP_K,
                "queryCount": len(rows),
                "queries": rows,
                "modelId": backend.model_id,
                "dimension": backend.dimension,
                "receiptBudgetChars": MAX_RECEIPT_JSON_CHARS,
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
