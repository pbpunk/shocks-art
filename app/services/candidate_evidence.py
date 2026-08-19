from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.indexing.language_traces import LanguageSegment, parse_youtube_json3_segments
from app.models import AnalysisRun, CandidateWindow, StreamTranscript
from app.schemas.candidate import CandidateResponse


DEFAULT_WINDOW_TOLERANCE_SECONDS = 5
DEFAULT_EVIDENCE_RADIUS_SECONDS = 8
MIN_SUPPORT_SCORE = 0.68
_NO_EVIDENCE_SENTINEL = "no verified in window transcript evidence"


class CandidateEvidenceValidationError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        summary = "; ".join(errors[:3])
        super().__init__(f"Candidate temporal transcript evidence failed: {summary}")


@dataclass(frozen=True)
class CandidateEvidenceAudit:
    candidate_id: str
    stream_id: str
    analysis_run_id: str
    model: str
    prompt_version: str
    title: str
    start_seconds: int
    end_seconds: int
    status: str
    transcript_available: bool
    issues: tuple[str, ...]

    def as_dict(self) -> dict:
        payload = asdict(self)
        payload["issues"] = list(self.issues)
        return payload


def _normalize_tokens(value: str) -> list[str]:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = text.replace("’", "'").replace("–", "-")
    text = re.sub(r"(?<=\d),(?=\d)", "", text)
    text = re.sub(r"\b(\d+)\s*v\b", r"\1 volts", text)
    text = re.sub(r"\b(\d+)\s*ma\b", r"\1 milliamps", text)
    return re.findall(r"[a-z0-9]+(?:'[a-z]+)?", text)


def _normalized(value: str) -> str:
    return " ".join(_normalize_tokens(value))


def _is_no_evidence_excerpt(value: str) -> bool:
    normalized = _normalized(value)
    return not normalized or normalized.startswith(_NO_EVIDENCE_SENTINEL)


def _support_score(claim: str, context: str) -> float:
    claim_tokens = _normalize_tokens(claim)
    context_tokens = _normalize_tokens(context)
    if not claim_tokens or not context_tokens:
        return 0.0

    normalized_claim = " ".join(claim_tokens)
    normalized_context = " ".join(context_tokens)
    if normalized_claim in normalized_context:
        return 1.0

    claim_counts = Counter(claim_tokens)
    extra = max(3, min(12, len(claim_tokens) // 2))
    best = 0.0
    for start in range(len(context_tokens)):
        for width in (len(claim_tokens), len(claim_tokens) + extra):
            window = context_tokens[start : start + width]
            if not window:
                continue
            sequence = SequenceMatcher(None, claim_tokens, window, autojunk=False).ratio()
            overlap = sum((claim_counts & Counter(window)).values()) / len(claim_tokens)
            best = max(best, (sequence * 0.65) + (overlap * 0.35))
    return best


def _segment_overlaps(segment: LanguageSegment, start_ms: int, end_ms: int) -> bool:
    segment_end = max(segment.start_ms, segment.end_ms)
    return segment.start_ms <= end_ms and segment_end >= start_ms


def _caption_context(
    segments: tuple[LanguageSegment, ...],
    *,
    start_seconds: int,
    end_seconds: int,
    tolerance_seconds: int,
) -> str:
    start_ms = max(0, (int(start_seconds) - tolerance_seconds) * 1000)
    end_ms = (int(end_seconds) + tolerance_seconds) * 1000
    return " ".join(segment.text for segment in segments if _segment_overlaps(segment, start_ms, end_ms))


def _evidence_values(evidence) -> tuple[int, str]:
    if isinstance(evidence, dict):
        return int(evidence.get("seconds", 0)), str(evidence.get("text", ""))
    return int(evidence.seconds), str(evidence.text)


def _candidate_issues(
    candidate,
    segments: tuple[LanguageSegment, ...],
    *,
    window_tolerance_seconds: int,
    evidence_radius_seconds: int,
) -> list[str]:
    title = str(candidate.title)
    excerpt = str(candidate.transcript_excerpt or "")
    evidence_items = list(candidate.transcript_evidence or [])
    issues: list[str] = []

    if not _is_no_evidence_excerpt(excerpt) and not evidence_items:
        issues.append(
            f"{title}: transcript_excerpt claims spoken evidence but transcript_evidence is empty"
        )

    for evidence in evidence_items:
        seconds, text = _evidence_values(evidence)
        context = _caption_context(
            segments,
            start_seconds=max(int(candidate.start_seconds), seconds - evidence_radius_seconds),
            end_seconds=min(int(candidate.end_seconds), seconds + evidence_radius_seconds),
            tolerance_seconds=window_tolerance_seconds,
        )
        score = _support_score(text, context)
        if score < MIN_SUPPORT_SCORE:
            issues.append(
                f"{title}: transcript_evidence at {seconds}s is not supported by stored captions "
                f"inside the candidate window (support={score:.3f}): {text[:180]}"
            )

    return issues


def _segments_for_transcript(transcript: StreamTranscript | None) -> tuple[LanguageSegment, ...] | None:
    if transcript is None or not transcript.raw_location:
        return None
    raw_path = Path(transcript.raw_location)
    if not raw_path.is_file():
        return None
    try:
        return parse_youtube_json3_segments(raw_path)
    except Exception:
        return None


def validate_candidate_transcript_evidence(
    response: CandidateResponse,
    transcript: StreamTranscript | None,
    *,
    window_tolerance_seconds: int = DEFAULT_WINDOW_TOLERANCE_SECONDS,
    evidence_radius_seconds: int = DEFAULT_EVIDENCE_RADIUS_SECONDS,
) -> CandidateResponse:
    """Reject transcript-backed candidates whose declared evidence cannot be verified.

    The schema already guarantees evidence timestamps fall inside the candidate window.
    This gate adds the missing semantic checks: spoken transcript excerpts require
    timestamped evidence, and each declared evidence line must be supported by the
    stored JSON3 captions near that in-window timestamp.

    When no stored timestamped transcript is available, the gate intentionally does
    not reject the response. Those candidates remain unverified until another speech
    evidence source (for example local transcription) exists.
    """

    segments = _segments_for_transcript(transcript)
    if segments is None:
        return response

    errors: list[str] = []
    for candidate in response.candidates:
        errors.extend(
            _candidate_issues(
                candidate,
                segments,
                window_tolerance_seconds=window_tolerance_seconds,
                evidence_radius_seconds=evidence_radius_seconds,
            )
        )
    if errors:
        raise CandidateEvidenceValidationError(errors)
    return response


def audit_candidate_window(
    candidate: CandidateWindow,
    transcript: StreamTranscript | None,
    *,
    window_tolerance_seconds: int = DEFAULT_WINDOW_TOLERANCE_SECONDS,
    evidence_radius_seconds: int = DEFAULT_EVIDENCE_RADIUS_SECONDS,
) -> CandidateEvidenceAudit:
    segments = _segments_for_transcript(transcript)
    if segments is None:
        status = "unverifiable"
        issues = ("timestamped raw transcript unavailable",)
    else:
        found = _candidate_issues(
            candidate,
            segments,
            window_tolerance_seconds=window_tolerance_seconds,
            evidence_radius_seconds=evidence_radius_seconds,
        )
        status = "fail" if found else "pass"
        issues = tuple(found)

    run = candidate.analysis_run
    return CandidateEvidenceAudit(
        candidate_id=candidate.candidate_window_id,
        stream_id=candidate.stream_id,
        analysis_run_id=candidate.analysis_run_id,
        model=run.model,
        prompt_version=run.prompt_version,
        title=candidate.title,
        start_seconds=candidate.start_seconds,
        end_seconds=candidate.end_seconds,
        status=status,
        transcript_available=segments is not None,
        issues=issues,
    )


def audit_candidate_windows(
    db: Session,
    *,
    limit: int = 100,
    direct_gemini_only: bool = True,
    candidate_id: str | None = None,
) -> list[CandidateEvidenceAudit]:
    query = select(CandidateWindow).join(AnalysisRun)
    if direct_gemini_only:
        query = query.where(AnalysisRun.model.like("gemini-%"))
    if candidate_id:
        query = query.where(CandidateWindow.candidate_window_id == candidate_id)
    query = query.order_by(CandidateWindow.created_at.desc()).limit(max(1, int(limit)))

    audits: list[CandidateEvidenceAudit] = []
    for candidate in db.scalars(query).all():
        transcript = db.scalar(
            select(StreamTranscript)
            .where(StreamTranscript.stream_id == candidate.stream_id)
            .order_by(StreamTranscript.updated_at.desc(), StreamTranscript.created_at.desc())
        )
        audits.append(audit_candidate_window(candidate, transcript))
    return audits
