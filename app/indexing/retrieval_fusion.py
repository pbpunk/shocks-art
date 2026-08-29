from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from app.indexing.language_search import LanguageSearchMatch
from app.indexing.visual_search import VisualSearchMatch


DEFAULT_MAX_GAP_MS = 120_000
DEFAULT_RRF_K = 60
DEFAULT_PROXIMITY_WEIGHT = 0.5


@dataclass(frozen=True)
class TemporalFusionMatch:
    media_id: str
    start_ms: int
    end_ms: int
    score: float
    language_trace_id: str
    language_trace_ids: tuple[str, ...]
    language_rank: int
    language_score: float
    language_text: str
    visual_trace_id: str
    visual_rank: int
    visual_score: float
    visual_artifact_path: str
    gap_ms: int
    proximity: float

    def as_dict(self) -> dict:
        return {
            "mediaId": self.media_id,
            "startMs": self.start_ms,
            "endMs": self.end_ms,
            "score": round(self.score, 8),
            "gapMs": self.gap_ms,
            "proximity": round(self.proximity, 8),
            "language": {
                "traceId": self.language_trace_id,
                "traceIds": list(self.language_trace_ids),
                "rank": self.language_rank,
                "score": round(self.language_score, 8),
                "text": self.language_text,
            },
            "visual": {
                "traceId": self.visual_trace_id,
                "rank": self.visual_rank,
                "score": round(self.visual_score, 8),
                "artifactPath": self.visual_artifact_path,
            },
        }


def temporal_gap_ms(
    left_start_ms: int,
    left_end_ms: int,
    right_start_ms: int,
    right_end_ms: int,
) -> int:
    """Return the non-negative gap between two timestamp intervals."""

    if left_end_ms < right_start_ms:
        return right_start_ms - left_end_ms
    if right_end_ms < left_start_ms:
        return left_start_ms - right_end_ms
    return 0


def _proximity_for_gap(gap_ms: int, max_gap_ms: int) -> float:
    # Keep temporal evidence monotonic but deliberately modest. Rank evidence remains
    # primary, and raw modality scores stay exposed rather than being normalized into
    # one opaque cross-modal score.
    return 1.0 - (gap_ms / max_gap_ms)


def fuse_temporal_retrieval(
    language_matches: Sequence[LanguageSearchMatch],
    visual_matches: Sequence[VisualSearchMatch],
    *,
    top_k: int = 10,
    max_gap_ms: int = DEFAULT_MAX_GAP_MS,
    rrf_k: int = DEFAULT_RRF_K,
    proximity_weight: float = DEFAULT_PROXIMITY_WEIGHT,
) -> tuple[TemporalFusionMatch, ...]:
    """Rank same-Media language/visual neighborhoods without mixing raw score scales.

    This is an evaluation primitive, not a production relevance policy. Language and
    visual raw scores are intentionally incomparable, so reciprocal ranks provide the
    common ordering signal. Temporal proximity contributes only when evidence belongs
    to the same Media item and falls within ``max_gap_ms``. Filename, title, source
    path, and all other Media metadata are absent by construction.

    One best language neighborhood is retained for each visual Trace. This makes the
    fused result a set of inspectable visual moments with nearby spoken evidence rather
    than a Cartesian product of nearly identical timestamp pairs.
    """

    if top_k <= 0:
        raise ValueError("top_k must be greater than zero")
    if max_gap_ms <= 0:
        raise ValueError("max_gap_ms must be greater than zero")
    if rrf_k < 0:
        raise ValueError("rrf_k must not be negative")
    if proximity_weight < 0:
        raise ValueError("proximity_weight must not be negative")

    language_by_media: dict[str, list[tuple[int, LanguageSearchMatch]]] = {}
    for language_rank, language in enumerate(language_matches, start=1):
        language_by_media.setdefault(language.media_id, []).append((language_rank, language))

    fused: list[TemporalFusionMatch] = []
    for visual_rank, visual in enumerate(visual_matches, start=1):
        best: TemporalFusionMatch | None = None
        for language_rank, language in language_by_media.get(visual.media_id, ()):  # same Media only
            gap_ms = temporal_gap_ms(
                language.start_ms,
                language.end_ms,
                visual.start_ms,
                visual.end_ms,
            )
            if gap_ms > max_gap_ms:
                continue

            proximity = _proximity_for_gap(gap_ms, max_gap_ms)
            language_rrf = 1.0 / (rrf_k + language_rank)
            visual_rrf = 1.0 / (rrf_k + visual_rank)
            agreement = min(language_rrf, visual_rrf) * proximity * proximity_weight
            score = language_rrf + visual_rrf + agreement
            candidate = TemporalFusionMatch(
                media_id=visual.media_id,
                start_ms=min(language.start_ms, visual.start_ms),
                end_ms=max(language.end_ms, visual.end_ms),
                score=score,
                language_trace_id=language.trace_id,
                language_trace_ids=language.trace_ids,
                language_rank=language_rank,
                language_score=language.score,
                language_text=language.text,
                visual_trace_id=visual.trace_id,
                visual_rank=visual_rank,
                visual_score=visual.score,
                visual_artifact_path=visual.artifact_path,
                gap_ms=gap_ms,
                proximity=proximity,
            )
            if best is None:
                best = candidate
                continue
            candidate_key = (
                -candidate.score,
                candidate.gap_ms,
                candidate.language_rank,
                candidate.language_trace_id,
            )
            best_key = (
                -best.score,
                best.gap_ms,
                best.language_rank,
                best.language_trace_id,
            )
            if candidate_key < best_key:
                best = candidate
        if best is not None:
            fused.append(best)

    fused.sort(
        key=lambda item: (
            -item.score,
            item.gap_ms,
            item.visual_rank,
            item.language_rank,
            item.visual_trace_id,
            item.language_trace_id,
        )
    )
    return tuple(fused[:top_k])
