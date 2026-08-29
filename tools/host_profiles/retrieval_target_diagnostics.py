from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import func, select

LIVE_ROOT = Path(os.getenv("SHOCKS_HOST_LIVE_ROOT", Path(__file__).resolve().parents[2])).resolve()
os.chdir(LIVE_ROOT)
if str(LIVE_ROOT) not in sys.path:
    sys.path.insert(0, str(LIVE_ROOT))

from app.core.database import SessionLocal
from app.indexing.language_search import LanguageSearchMatch, search_language_traces
from app.indexing.qwen_query_backend import QwenPersistentQueryEmbeddingBackend
from app.indexing.retrieval_fusion import temporal_gap_ms
from app.indexing.visual_search import VisualSearchMatch, search_visual_embeddings
from app.library_models import Embedding, Media, Trace


TARGETS: tuple[dict[str, str], ...] = (
    {
        "queryId": "fractal-burning-setup",
        "query": "fractal burning setup",
        "mediaId": "media_4a2b9b61b1cd44e7bd820ed68dbf207d",
    },
    {
        "queryId": "finished-staffs",
        "query": "finished staffs",
        "mediaId": "media_0a571dc5e48942fc9b9d98e27609eeb0",
    },
    {
        "queryId": "gluing-sign-control",
        "query": "gluing letters onto a sign",
        "mediaId": "media_53c498d982c14ec680bacf2be2f4dfa0",
    },
)
LANGUAGE_K = 500
VISUAL_K = 5000
ANCHOR_LIMIT = 5
CANDIDATE_DEPTHS = (25, 50, 100)


def emit(payload: dict[str, Any], code: int = 0) -> int:
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    return code


def _ranked_target(matches: Iterable[Any], media_id: str) -> list[tuple[int, Any]]:
    return [
        (rank, match)
        for rank, match in enumerate(matches, start=1)
        if match.media_id == media_id
    ]


def _nearest_visual(
    language: LanguageSearchMatch,
    ranked_visual: list[tuple[int, VisualSearchMatch]],
) -> tuple[int, VisualSearchMatch, int] | None:
    if not ranked_visual:
        return None
    rank, match = min(
        ranked_visual,
        key=lambda item: (
            temporal_gap_ms(
                language.start_ms,
                language.end_ms,
                item[1].start_ms,
                item[1].end_ms,
            ),
            item[0],
        ),
    )
    return (
        rank,
        match,
        temporal_gap_ms(
            language.start_ms,
            language.end_ms,
            match.start_ms,
            match.end_ms,
        ),
    )


def _nearest_language(
    visual: VisualSearchMatch,
    ranked_language: list[tuple[int, LanguageSearchMatch]],
) -> tuple[int, LanguageSearchMatch, int] | None:
    if not ranked_language:
        return None
    rank, match = min(
        ranked_language,
        key=lambda item: (
            temporal_gap_ms(
                item[1].start_ms,
                item[1].end_ms,
                visual.start_ms,
                visual.end_ms,
            ),
            item[0],
        ),
    )
    return (
        rank,
        match,
        temporal_gap_ms(
            match.start_ms,
            match.end_ms,
            visual.start_ms,
            visual.end_ms,
        ),
    )


def _depth_flags(rank: int) -> dict[str, bool]:
    return {f"withinTop{depth}": rank <= depth for depth in CANDIDATE_DEPTHS}


def _language_anchor_payload(
    rank: int,
    match: LanguageSearchMatch,
    ranked_visual: list[tuple[int, VisualSearchMatch]],
) -> dict[str, Any]:
    nearest = _nearest_visual(match, ranked_visual)
    nearest_payload: dict[str, Any] | None = None
    if nearest is not None:
        visual_rank, visual, gap_ms = nearest
        nearest_payload = {
            "globalRank": visual_rank,
            "traceId": visual.trace_id,
            "startMs": visual.start_ms,
            "endMs": visual.end_ms,
            "score": round(visual.score, 8),
            "gapMs": gap_ms,
            **_depth_flags(visual_rank),
        }
    return {
        "globalRank": rank,
        "traceId": match.trace_id,
        "startMs": match.start_ms,
        "endMs": match.end_ms,
        "score": round(match.score, 8),
        "matchedTerms": list(match.matched_terms),
        "text": match.text[:320],
        **_depth_flags(rank),
        "nearestVisual": nearest_payload,
    }


def _visual_anchor_payload(
    rank: int,
    match: VisualSearchMatch,
    ranked_language: list[tuple[int, LanguageSearchMatch]],
) -> dict[str, Any]:
    nearest = _nearest_language(match, ranked_language)
    nearest_payload: dict[str, Any] | None = None
    if nearest is not None:
        language_rank, language, gap_ms = nearest
        nearest_payload = {
            "globalRank": language_rank,
            "traceId": language.trace_id,
            "startMs": language.start_ms,
            "endMs": language.end_ms,
            "score": round(language.score, 8),
            "matchedTerms": list(language.matched_terms),
            "text": language.text[:320],
            "gapMs": gap_ms,
            **_depth_flags(language_rank),
        }
    return {
        "globalRank": rank,
        "traceId": match.trace_id,
        "startMs": match.start_ms,
        "endMs": match.end_ms,
        "score": round(match.score, 8),
        **_depth_flags(rank),
        "nearestLanguage": nearest_payload,
    }


def _best_temporal_pair(
    ranked_language: list[tuple[int, LanguageSearchMatch]],
    ranked_visual: list[tuple[int, VisualSearchMatch]],
) -> dict[str, Any] | None:
    if not ranked_language or not ranked_visual:
        return None
    language_rank, language, visual_rank, visual, gap_ms = min(
        (
            (language_rank, language, visual_rank, visual, temporal_gap_ms(
                language.start_ms,
                language.end_ms,
                visual.start_ms,
                visual.end_ms,
            ))
            for language_rank, language in ranked_language
            for visual_rank, visual in ranked_visual
        ),
        key=lambda item: (item[4], item[0] + item[2], item[0], item[2]),
    )
    return {
        "gapMs": gap_ms,
        "language": {
            "globalRank": language_rank,
            "traceId": language.trace_id,
            "startMs": language.start_ms,
            "endMs": language.end_ms,
            "score": round(language.score, 8),
            "matchedTerms": list(language.matched_terms),
            "text": language.text[:320],
            **_depth_flags(language_rank),
        },
        "visual": {
            "globalRank": visual_rank,
            "traceId": visual.trace_id,
            "startMs": visual.start_ms,
            "endMs": visual.end_ms,
            "score": round(visual.score, 8),
            **_depth_flags(visual_rank),
        },
    }


def _exact_visual_count(db, *, media_id: str, model_id: str, dimension: int) -> int:
    return int(
        db.scalar(
            select(func.count(Embedding.embedding_id))
            .join(Trace, Embedding.trace_id == Trace.trace_id)
            .where(
                Trace.media_id == media_id,
                Trace.trace_type == "visual",
                Embedding.model_id == model_id,
                Embedding.embedding_dimension == dimension,
                Embedding.normalized.is_(True),
            )
        )
        or 0
    )


def main() -> int:
    started = time.perf_counter()
    backend = QwenPersistentQueryEmbeddingBackend()
    try:
        vectors = backend.embed_text([target["query"] for target in TARGETS])
        if len(vectors) != len(TARGETS):
            raise RuntimeError(
                f"Qwen returned {len(vectors)} vectors for {len(TARGETS)} fixed diagnostics queries"
            )

        results: list[dict[str, Any]] = []
        with SessionLocal() as db:
            for target, vector in zip(TARGETS, vectors, strict=True):
                media_id = target["mediaId"]
                media = db.get(Media, media_id)
                if media is None:
                    return emit({"summary": "Fixed retrieval diagnostic Media is not present", "queryId": target["queryId"]}, 1)
                if media.source_type != "youtube":
                    return emit({"summary": "Fixed retrieval diagnostic Media is not canonical YouTube Media", "queryId": target["queryId"]}, 1)

                language = search_language_traces(
                    db,
                    query=target["query"],
                    top_k=LANGUAGE_K,
                )
                visual = search_visual_embeddings(
                    db,
                    query_vector=vector,
                    model_id=backend.model_id,
                    dimension=backend.dimension,
                    top_k=VISUAL_K,
                )
                ranked_language = _ranked_target(language.matches, media_id)
                ranked_visual = _ranked_target(visual.matches, media_id)
                if not ranked_language or not ranked_visual:
                    return emit(
                        {
                            "summary": "Fixed retrieval diagnostic target lacks query-visible evidence",
                            "queryId": target["queryId"],
                            "targetLanguageMatches": len(ranked_language),
                            "targetVisualMatches": len(ranked_visual),
                        },
                        1,
                    )

                results.append(
                    {
                        "queryId": target["queryId"],
                        "query": target["query"],
                        "mediaId": media_id,
                        "languageGlobalCount": len(language.matches),
                        "visualGlobalCount": len(visual.matches),
                        "targetLanguageMatchCount": len(ranked_language),
                        "targetVisualMatchCount": len(ranked_visual),
                        "exactGenerationVisualEmbeddings": _exact_visual_count(
                            db,
                            media_id=media_id,
                            model_id=backend.model_id,
                            dimension=backend.dimension,
                        ),
                        "primaryLanguageAnchor": _language_anchor_payload(
                            *ranked_language[0],
                            ranked_visual,
                        ),
                        "primaryVisualAnchor": _visual_anchor_payload(
                            *ranked_visual[0],
                            ranked_language,
                        ),
                        "languageAnchors": [
                            _language_anchor_payload(rank, match, ranked_visual)
                            for rank, match in ranked_language[:ANCHOR_LIMIT]
                        ],
                        "visualAnchors": [
                            _visual_anchor_payload(rank, match, ranked_language)
                            for rank, match in ranked_visual[:ANCHOR_LIMIT]
                        ],
                        "bestTemporalPair": _best_temporal_pair(ranked_language, ranked_visual),
                    }
                )

        return emit(
            {
                "summary": "Fixed retrieval target diagnostics completed",
                "modelId": backend.model_id,
                "dimension": backend.dimension,
                "languageDepth": LANGUAGE_K,
                "visualDepth": VISUAL_K,
                "candidateDepths": list(CANDIDATE_DEPTHS),
                "targetCount": len(results),
                "targets": results,
                "metadataUsedForSelectionOrScoring": False,
                "stateMutationRequested": False,
                "durationSeconds": round(time.perf_counter() - started, 3),
            }
        )
    except Exception as exc:
        return emit(
            {
                "summary": f"Fixed retrieval target diagnostics failed: {type(exc).__name__}: {exc}",
                "durationSeconds": round(time.perf_counter() - started, 3),
            },
            1,
        )


if __name__ == "__main__":
    raise SystemExit(main())
