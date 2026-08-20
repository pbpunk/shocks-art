from __future__ import annotations

from app.models import StreamTranscript
from app.schemas.candidate import CandidateResponse, TimedEvidence, seconds_to_timestamp
from app.services.candidate_evidence import (
    MIN_SUPPORT_SCORE,
    CandidateEvidenceValidationError,
    _claim_chunks,
    _is_no_evidence_excerpt,
    _segments_for_transcript,
    _support_score,
)

_MAX_EVIDENCE_WINDOW_SECONDS = 30
_MAX_EVIDENCE_WINDOW_SEGMENTS = 12
_GROUNDING_VERSION = "caption-grounding-v1"


def _segments_inside_candidate(candidate, segments):
    start_ms = int(candidate.start_seconds) * 1000
    end_ms = int(candidate.end_seconds) * 1000
    return tuple(
        segment
        for segment in segments
        if start_ms <= segment.start_ms <= end_ms
    )


def _best_evidence_window(claim: str, segments):
    best = None
    best_score = 0.0
    best_text_length = None

    for start_index, first in enumerate(segments):
        parts: list[str] = []
        for end_index in range(
            start_index,
            min(len(segments), start_index + _MAX_EVIDENCE_WINDOW_SEGMENTS),
        ):
            current = segments[end_index]
            if current.start_ms - first.start_ms > _MAX_EVIDENCE_WINDOW_SECONDS * 1000:
                break
            parts.append(current.text)
            text = " ".join(parts).strip()
            score = _support_score(claim, text)
            text_length = len(text)
            if score > best_score or (
                score == best_score
                and best is not None
                and best_text_length is not None
                and text_length < best_text_length
            ):
                best = (first, text)
                best_score = score
                best_text_length = text_length

    return best, best_score


def ground_missing_transcript_evidence(
    response: CandidateResponse,
    transcript: StreamTranscript | None,
) -> CandidateResponse:
    """Attach source-backed timed evidence when Gemini omitted bookkeeping.

    This does not relax the temporal gate. A spoken transcript_excerpt with no declared
    transcript_evidence is grounded only when every auditable excerpt chunk can be
    matched to stored JSON3 captions whose timestamps begin inside the selected
    candidate window. The generated evidence text is copied from those captions and
    carries the caption start timestamp. If any chunk cannot be grounded, the candidate
    is rejected before persistence.
    """

    segments = _segments_for_transcript(transcript)
    if segments is None:
        return response

    errors: list[str] = []

    for candidate in response.candidates:
        excerpt = str(candidate.transcript_excerpt or "")
        if candidate.transcript_evidence or _is_no_evidence_excerpt(excerpt):
            continue

        chunks = _claim_chunks(excerpt)
        if not chunks:
            errors.append(
                f"{candidate.title}: transcript_evidence is empty and transcript_excerpt "
                "has no auditable spoken chunks"
            )
            continue

        candidate_segments = _segments_inside_candidate(candidate, segments)
        if not candidate_segments:
            errors.append(
                f"{candidate.title}: transcript_evidence is empty and no stored caption "
                "segments begin inside the candidate window"
            )
            continue

        grounded: list[TimedEvidence] = []
        candidate_errors: list[str] = []

        for chunk in chunks:
            match, score = _best_evidence_window(chunk, candidate_segments)
            if match is None or score < MIN_SUPPORT_SCORE:
                candidate_errors.append(
                    f"{candidate.title}: transcript_evidence is empty and transcript_excerpt "
                    f"could not be grounded inside the candidate window (support={score:.3f}): "
                    f"{chunk[:180]}"
                )
                continue

            first_segment, evidence_text = match
            seconds = first_segment.start_ms // 1000
            grounded.append(
                TimedEvidence(
                    timestamp=seconds_to_timestamp(seconds),
                    seconds=seconds,
                    text=evidence_text,
                )
            )

        if candidate_errors:
            errors.extend(candidate_errors)
            continue

        unique: list[TimedEvidence] = []
        seen: set[tuple[int, str]] = set()
        for evidence in grounded:
            key = (evidence.seconds, evidence.text)
            if key in seen:
                continue
            seen.add(key)
            unique.append(evidence)

        candidate.transcript_evidence = unique
        observations = dict(candidate.emergent_observations or {})
        observations["_transcript_evidence_grounding"] = {
            "version": _GROUNDING_VERSION,
            "source": "stored_json3_captions",
            "mode": "auto_from_transcript_excerpt",
            "evidenceCount": len(unique),
        }
        candidate.emergent_observations = observations

    if errors:
        raise CandidateEvidenceValidationError(errors)

    return response
