from __future__ import annotations

import math
import re
from dataclasses import dataclass

from app.models import StreamTranscript
from app.schemas.candidate import CandidateResponse, TimedEvidence, seconds_to_timestamp
from app.services.candidate_evidence import (
    MIN_SUPPORT_SCORE,
    CandidateEvidenceValidationError,
    _claim_chunks,
    _is_no_evidence_excerpt,
    _segments_for_transcript,
    _support_score,
    validate_candidate_transcript_evidence,
)
from app.services.candidate_grounding import _best_evidence_window, _segments_inside_candidate


_GROUNDING_VERSION = "native-ask-caption-grounding-v2"
_NO_VERIFIED_EVIDENCE = "No verified in-window transcript evidence"
_TIMESTAMP_RE = re.compile(r"\(?\b\d{1,2}:\d{2}(?::\d{2})?\b\)?")
_UNVERIFIED_MARKERS = (
    "no verified",
    "cannot verify",
    "can't verify",
    "unable to verify",
    "not verified",
)
_GLOBAL_MIN_SUPPORT_SCORE = max(MIN_SUPPORT_SCORE, 0.78)
_GLOBAL_MIN_MARGIN = 0.08
_GLOBAL_NONOVERLAP_SECONDS = 45
_MAX_EVIDENCE_WINDOW_SECONDS = 30
_MAX_EVIDENCE_WINDOW_SEGMENTS = 12


@dataclass(frozen=True)
class _GlobalMatch:
    first_segment: object
    text: str
    score: float
    runner_up_score: float


def _append_risk(candidate, risk: str) -> None:
    risks = list(candidate.risks or [])
    if risk not in risks:
        risks.append(risk)
    candidate.risks = risks


def _usable_model_claim(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    lowered = text.casefold()
    if any(marker in lowered for marker in _UNVERIFIED_MARKERS):
        return ""
    return _TIMESTAMP_RE.sub(" ", text).strip()


def _claims_for_candidate(candidate) -> list[str]:
    observations = dict(candidate.emergent_observations or {})
    exact_quote = _usable_model_claim(str(observations.get("exact_caption_quote") or ""))
    transcript_excerpt = _usable_model_claim(str(candidate.transcript_excerpt or ""))

    claims: list[str] = []
    seen: set[str] = set()
    for source in (exact_quote, transcript_excerpt):
        for chunk in _claim_chunks(source):
            if chunk in seen:
                continue
            seen.add(chunk)
            claims.append(chunk)
    return claims


def _set_grounding_observation(candidate, **payload) -> None:
    observations = dict(candidate.emergent_observations or {})
    observations["_transcript_evidence_grounding"] = {
        "version": _GROUNDING_VERSION,
        "source": "stored_json3_captions",
        **payload,
    }
    candidate.emergent_observations = observations


def _candidate_global_windows(claim: str, segments) -> list[tuple[object, str, float]]:
    windows: list[tuple[object, str, float]] = []
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
            if score:
                windows.append((first, text, score))
    windows.sort(key=lambda item: (-item[2], len(item[1]), item[0].start_ms))
    return windows


def _unambiguous_global_match(claim: str, segments) -> _GlobalMatch | None:
    windows = _candidate_global_windows(claim, segments)
    if not windows:
        return None

    best_segment, best_text, best_score = windows[0]
    if best_score < _GLOBAL_MIN_SUPPORT_SCORE:
        return None

    runner_up_score = 0.0
    best_start_ms = best_segment.start_ms
    for segment, _text, score in windows[1:]:
        if abs(segment.start_ms - best_start_ms) < _GLOBAL_NONOVERLAP_SECONDS * 1000:
            continue
        runner_up_score = score
        break

    if runner_up_score and (best_score - runner_up_score) < _GLOBAL_MIN_MARGIN:
        return None

    return _GlobalMatch(
        first_segment=best_segment,
        text=best_text,
        score=best_score,
        runner_up_score=runner_up_score,
    )


def _shift_timed_evidence(items, delta_seconds: int) -> list[TimedEvidence]:
    shifted: list[TimedEvidence] = []
    for item in list(items or []):
        seconds = int(item.seconds) + delta_seconds
        if seconds < 0:
            continue
        shifted.append(
            TimedEvidence(
                timestamp=seconds_to_timestamp(seconds),
                seconds=seconds,
                text=str(item.text),
            )
        )
    return shifted


def _reanchor_candidate_window(candidate, matches: list[_GlobalMatch], segments) -> None:
    duration = int(candidate.duration_seconds)
    match_seconds = [int(match.first_segment.start_ms // 1000) for match in matches]
    earliest = min(match_seconds)
    latest = max(match_seconds)
    if latest - earliest > duration:
        raise CandidateEvidenceValidationError(
            [
                f"{candidate.title}: globally matched speech claims span {latest - earliest}s, "
                f"which does not fit inside the proposed {duration}s candidate duration"
            ]
        )

    old_start = int(candidate.start_seconds)
    old_end = int(candidate.end_seconds)
    midpoint = (earliest + latest) / 2.0
    new_start = max(0, int(round(midpoint - (duration / 2.0))))
    transcript_end = max(int(math.ceil(segment.end_ms / 1000)) for segment in segments)
    if transcript_end >= duration:
        new_start = min(new_start, transcript_end - duration)
    if earliest < new_start:
        new_start = earliest
    if latest > new_start + duration:
        new_start = latest - duration
    new_start = max(0, new_start)
    new_end = new_start + duration
    delta = new_start - old_start

    candidate.start_seconds = new_start
    candidate.end_seconds = new_end
    candidate.start_timestamp = seconds_to_timestamp(new_start)
    candidate.end_timestamp = seconds_to_timestamp(new_end)
    candidate.visual_evidence = _shift_timed_evidence(candidate.visual_evidence, delta)
    _append_risk(candidate, "native_ask_window_reanchored")
    _set_grounding_observation(
        candidate,
        mode="reanchored_from_global_caption_match",
        originalWindow={"startSeconds": old_start, "endSeconds": old_end},
        reanchoredWindow={"startSeconds": new_start, "endSeconds": new_end},
        claimCount=len(matches),
        minimumSupport=round(min(match.score for match in matches), 3),
        minimumSeparation=round(
            min(
                match.score - match.runner_up_score
                if match.runner_up_score
                else match.score
                for match in matches
            ),
            3,
        ),
    )


def _ground_claims_in_segments(candidate, claims: list[str], candidate_segments):
    grounded: list[TimedEvidence] = []
    errors: list[str] = []
    support_scores: list[float] = []
    for claim in claims:
        match, score = _best_evidence_window(claim, candidate_segments)
        if match is None or score < MIN_SUPPORT_SCORE:
            errors.append(
                f"{candidate.title}: native Ask speech claim could not be grounded inside the proposed "
                f"candidate window (support={score:.3f}): {claim[:180]}"
            )
            continue
        first_segment, evidence_text = match
        seconds = first_segment.start_ms // 1000
        support_scores.append(score)
        grounded.append(
            TimedEvidence(
                timestamp=seconds_to_timestamp(seconds),
                seconds=seconds,
                text=evidence_text,
            )
        )
    return grounded, errors, support_scores


def ground_native_ask_transcript_evidence(
    response: CandidateResponse,
    transcript: StreamTranscript | None,
) -> CandidateResponse:
    """Replace native-Ask speech bookkeeping with source-backed caption traces.

    YouTube Ask remains the editorial proposer. Its timestamp labels are not evidence.
    Claims are first matched inside the proposed CandidateWindow. When that fails, the
    full stored transcript may repair the window only if every claim has a strong,
    unambiguous global match and those matches still fit inside the original proposed
    duration. Ambiguous, hallucinated, or temporally incompatible claims fail closed.

    When timestamped captions are unavailable, speech claims are removed from the
    candidate card and the candidate is marked for verification instead of displaying
    ungrounded model text as if it were source evidence.
    """

    segments = _segments_for_transcript(transcript)
    if segments is None:
        for candidate in response.candidates:
            claims = _claims_for_candidate(candidate)
            if not claims or _is_no_evidence_excerpt(str(candidate.transcript_excerpt or "")):
                continue
            candidate.transcript_evidence = []
            candidate.transcript_excerpt = _NO_VERIFIED_EVIDENCE
            _append_risk(candidate, "caption_grounding_unavailable")
            _set_grounding_observation(
                candidate,
                mode="unavailable",
                claimCount=len(claims),
                evidenceCount=0,
            )
        return response

    errors: list[str] = []

    for candidate in response.candidates:
        claims = _claims_for_candidate(candidate)
        if not claims:
            if candidate.transcript_excerpt and not _is_no_evidence_excerpt(candidate.transcript_excerpt):
                candidate.transcript_evidence = []
                candidate.transcript_excerpt = _NO_VERIFIED_EVIDENCE
                _append_risk(candidate, "caption_claim_not_groundable")
            continue

        candidate_segments = _segments_inside_candidate(candidate, segments)
        grounded, candidate_errors, support_scores = _ground_claims_in_segments(
            candidate,
            claims,
            candidate_segments,
        ) if candidate_segments else ([], [f"{candidate.title}: no stored caption traces begin inside the proposed candidate window"], [])

        reanchored = False
        if candidate_errors:
            global_matches: list[_GlobalMatch] = []
            global_errors: list[str] = []
            for claim in claims:
                match = _unambiguous_global_match(claim, segments)
                if match is None:
                    global_errors.append(
                        f"{candidate.title}: native Ask speech claim has no strong unambiguous full-transcript match: {claim[:180]}"
                    )
                    continue
                global_matches.append(match)

            if global_errors:
                errors.extend(global_errors)
                continue

            try:
                _reanchor_candidate_window(candidate, global_matches, segments)
            except CandidateEvidenceValidationError as exc:
                errors.extend(exc.errors)
                continue

            candidate_segments = _segments_inside_candidate(candidate, segments)
            grounded, candidate_errors, support_scores = _ground_claims_in_segments(
                candidate,
                claims,
                candidate_segments,
            )
            if candidate_errors:
                errors.extend(candidate_errors)
                continue
            reanchored = True

        unique: list[TimedEvidence] = []
        seen: set[tuple[int, str]] = set()
        for evidence in grounded:
            key = (evidence.seconds, evidence.text)
            if key in seen:
                continue
            seen.add(key)
            unique.append(evidence)

        candidate.transcript_evidence = unique
        if unique:
            first = unique[0]
            candidate.transcript_excerpt = f'"{first.text}" ({first.timestamp})'
        else:
            candidate.transcript_excerpt = _NO_VERIFIED_EVIDENCE

        if not reanchored:
            _set_grounding_observation(
                candidate,
                mode="canonicalized_model_claims",
                claimCount=len(claims),
                evidenceCount=len(unique),
                minimumSupport=round(min(support_scores), 3) if support_scores else 0.0,
            )
        else:
            observations = dict(candidate.emergent_observations or {})
            payload = dict(observations.get("_transcript_evidence_grounding") or {})
            payload["evidenceCount"] = len(unique)
            observations["_transcript_evidence_grounding"] = payload
            candidate.emergent_observations = observations

    if errors:
        raise CandidateEvidenceValidationError(errors)

    return validate_candidate_transcript_evidence(response, transcript)
