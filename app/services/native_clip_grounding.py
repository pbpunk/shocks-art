from __future__ import annotations

import re

from app.models import StreamTranscript
from app.schemas.candidate import CandidateResponse, TimedEvidence, seconds_to_timestamp
from app.services.candidate_evidence import (
    MIN_SUPPORT_SCORE,
    CandidateEvidenceValidationError,
    _claim_chunks,
    _is_no_evidence_excerpt,
    _segments_for_transcript,
    validate_candidate_transcript_evidence,
)
from app.services.candidate_grounding import _best_evidence_window, _segments_inside_candidate


_GROUNDING_VERSION = "native-ask-caption-grounding-v1"
_NO_VERIFIED_EVIDENCE = "No verified in-window transcript evidence"
_TIMESTAMP_RE = re.compile(r"\(?\b\d{1,2}:\d{2}(?::\d{2})?\b\)?")
_UNVERIFIED_MARKERS = (
    "no verified",
    "cannot verify",
    "can't verify",
    "unable to verify",
    "not verified",
)


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


def ground_native_ask_transcript_evidence(
    response: CandidateResponse,
    transcript: StreamTranscript | None,
) -> CandidateResponse:
    """Replace native-Ask speech bookkeeping with source-backed caption traces.

    YouTube Ask remains the editorial proposer. Its timestamp labels are not evidence.
    Every auditable speech claim is matched against stored JSON3 caption traces inside
    the proposed CandidateWindow. The persisted evidence text and timestamp are copied
    from the matching source captions. Claims found only elsewhere in the video fail
    closed before CandidateWindow persistence.

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
        if not candidate_segments:
            errors.append(
                f"{candidate.title}: no stored caption traces begin inside the proposed candidate window"
            )
            continue

        grounded: list[TimedEvidence] = []
        candidate_errors: list[str] = []
        support_scores: list[float] = []

        for claim in claims:
            match, score = _best_evidence_window(claim, candidate_segments)
            if match is None or score < MIN_SUPPORT_SCORE:
                candidate_errors.append(
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
        if unique:
            first = unique[0]
            candidate.transcript_excerpt = f'"{first.text}" ({first.timestamp})'
        else:
            candidate.transcript_excerpt = _NO_VERIFIED_EVIDENCE

        _set_grounding_observation(
            candidate,
            mode="canonicalized_model_claims",
            claimCount=len(claims),
            evidenceCount=len(unique),
            minimumSupport=round(min(support_scores), 3) if support_scores else 0.0,
        )

    if errors:
        raise CandidateEvidenceValidationError(errors)

    return validate_candidate_transcript_evidence(response, transcript)
