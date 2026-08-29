from __future__ import annotations

from typing import Any, Iterable

from app.indexing.language_search import LanguageSearchMatch
from app.indexing.retrieval_fusion import temporal_gap_ms
from app.indexing.visual_search import VisualSearchMatch


CANDIDATE_DEPTHS = (25, 50, 100)


def ranked_target(matches: Iterable[Any], media_id: str) -> list[tuple[int, Any]]:
    return [
        (rank, match)
        for rank, match in enumerate(matches, start=1)
        if match.media_id == media_id
    ]


def nearest_visual(
    language: LanguageSearchMatch,
    ranked_visual_matches: list[tuple[int, VisualSearchMatch]],
) -> tuple[int, VisualSearchMatch, int] | None:
    if not ranked_visual_matches:
        return None
    rank, match = min(
        ranked_visual_matches,
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


def nearest_language(
    visual: VisualSearchMatch,
    ranked_language_matches: list[tuple[int, LanguageSearchMatch]],
) -> tuple[int, LanguageSearchMatch, int] | None:
    if not ranked_language_matches:
        return None
    rank, match = min(
        ranked_language_matches,
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


def depth_flags(rank: int) -> dict[str, bool]:
    return {f"withinTop{depth}": rank <= depth for depth in CANDIDATE_DEPTHS}
