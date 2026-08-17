import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import ROOT_DIR
from app.models import AnalysisRun, CandidateWindow, Stream
from app.schemas.candidate import CandidatePayload, CandidateResponse, seconds_to_timestamp
from app.services.native_youtube import timestamp_to_seconds
from app.services.ranking import weighted_score
from app.services.stream_archive import ensure_stream_transcript, save_structured_pass_artifacts
from app.services.tags import normalize_tags


STRUCTURED_MODEL = "native-youtube-structured-v1"
STRUCTURED_PROMPT_VERSION = "native-youtube-structured-1.0"


@dataclass(frozen=True)
class StructuredImportResult:
    run: AnalysisRun
    candidates: list[CandidateWindow]
    skipped_duplicates: int


def import_structured_pass(db: Session, run_dir: Path) -> StructuredImportResult:
    final_path = run_dir / "final.txt"
    review_path = run_dir / "local_review.json"
    if not final_path.exists() or not review_path.exists():
        raise ValueError(f"Structured pass is missing final.txt or local_review.json: {run_dir}")

    video_id = _video_id_from_run_dir(run_dir)
    stream = db.scalar(select(Stream).where(Stream.source_video_id == video_id))
    if not stream:
        raise ValueError(f"No stream exists for source video ID {video_id}")

    review = json.loads(review_path.read_text(encoding="utf-8"))
    ensure_stream_transcript(db, stream, fetch_missing=False)
    payloads = [
        _payload_from_review_candidate(stream, candidate)
        for candidate in review.get("candidates", [])
    ]
    if not payloads:
        raise ValueError(f"No structured candidates parsed from {run_dir}")

    run = AnalysisRun(
        stream_id=stream.stream_id,
        model=STRUCTURED_MODEL,
        prompt_version=STRUCTURED_PROMPT_VERSION,
        schema_version="1.0",
        status="processing",
        request_started_at=datetime.now(timezone.utc),
        raw_response_location=str(final_path),
        usage={"structured_pass_dir": str(run_dir)},
    )
    db.add(run)
    db.flush()

    response = CandidateResponse(
        schema_version="1.0",
        stream_id=stream.stream_id,
        source_video_id=stream.source_video_id,
        candidates=payloads,
    )
    candidates = []
    skipped_duplicates = 0
    for rank, payload in enumerate(response.candidates, start=1):
        existing = _find_duplicate(db, stream, payload, STRUCTURED_MODEL)
        if existing:
            skipped_duplicates += 1
            continue
        candidates.append(_candidate_from_payload(run, payload, rank))

    db.add_all(candidates)
    run.status = "complete"
    run.request_completed_at = datetime.now(timezone.utc)
    save_structured_pass_artifacts(db, stream, run, run_dir)
    run.exception_message = (
        f"Imported {len(candidates)} structured candidate(s); skipped {skipped_duplicates} duplicate(s)."
        if skipped_duplicates
        else ""
    )
    stream.processing_status = "complete"
    db.flush()
    return StructuredImportResult(run=run, candidates=candidates, skipped_duplicates=skipped_duplicates)


def latest_structured_pass_dirs(limit: int | None = None) -> list[Path]:
    root = ROOT_DIR / "data" / "structured_passes"
    if not root.exists():
        return []
    dirs = [path for path in root.iterdir() if path.is_dir() and (path / "local_review.json").exists()]
    dirs.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return dirs[:limit] if limit else dirs


def _video_id_from_run_dir(run_dir: Path) -> str:
    prompts_path = run_dir / "prompts.json"
    if prompts_path.exists():
        prompts = json.loads(prompts_path.read_text(encoding="utf-8"))
        for prompt in prompts.values():
            match = re.search(r"[?&]v=([^&\s]+)", prompt)
            if match:
                return match.group(1)
    match = re.match(r"(.+)_\d{8}_\d{6}$", run_dir.name)
    if match:
        return match.group(1)
    return run_dir.name


def _payload_from_review_candidate(stream: Stream, candidate: dict[str, Any]) -> CandidatePayload:
    start_seconds, end_seconds = _parse_range(candidate["timestamp_range"])
    exact_quote = candidate.get("exact_caption_quote", "")
    risks = list(candidate.get("local_flags", []))
    start_seconds, quote_note, quote_verified = _adjust_start_to_caption_quote(
        stream.source_video_id,
        exact_quote,
        start_seconds,
        end_seconds,
    )
    if quote_note:
        if quote_note != "caption_quote_verified":
            risks.append(quote_note)
    if not quote_verified:
        risks.append("caption_quote_needs_verification")

    duration = end_seconds - start_seconds
    score = _score(candidate)
    scores = _scores(candidate, score)
    primary_pillar = _pillar(candidate)
    window_type = _clean(candidate.get("window_type", "short_ready"))
    local_recommendation = candidate.get("local_recommendation", "")
    return CandidatePayload(
        title=_clean(candidate["title"]),
        start_seconds=start_seconds,
        end_seconds=end_seconds,
        start_timestamp=seconds_to_timestamp(start_seconds),
        end_timestamp=seconds_to_timestamp(end_seconds),
        duration_seconds=duration,
        concise_summary=_clean(candidate.get("summary", "Structured native Ask candidate.")),
        selection_reason=_clean(candidate.get("why_it_beats_alternatives", "Selected by structured native Ask pass.")),
        primary_pillar=primary_pillar,
        secondary_pillars=[],
        tags=_tags(primary_pillar, window_type, candidate),
        transcript_excerpt=exact_quote,
        visual_description=_clean(candidate.get("visual_evidence", "")),
        transcript_evidence=[
            {"timestamp": seconds_to_timestamp(start_seconds), "seconds": start_seconds, "text": exact_quote}
        ]
        if exact_quote
        else [],
        visual_evidence=[
            {
                "timestamp": seconds_to_timestamp(start_seconds),
                "seconds": start_seconds,
                "text": _clean(candidate.get("visual_evidence", "")),
            }
        ]
        if candidate.get("visual_evidence")
        else [],
        contextual_notes=_contextual_notes(candidate, quote_note, quote_verified),
        estimated_short_count=1 if window_type != "source_window" else 2,
        possible_hooks=[_clean(candidate.get("hook", ""))] if candidate.get("hook") else [],
        editing_notes=[_clean(candidate.get("editing_notes", ""))] if candidate.get("editing_notes") else [],
        risks=normalize_tags(risks),
        scores=scores,
        emergent_observations={
            "source": STRUCTURED_MODEL,
            "structured_prompt_version": STRUCTURED_PROMPT_VERSION,
            "window_type": window_type,
            "chatter_risk": candidate.get("chatter_risk", ""),
            "complete_thought": candidate.get("complete_thought", ""),
            "payoff_inside_window": candidate.get("payoff_inside_window", ""),
            "exact_quote_inside_window": candidate.get("exact_quote_inside_window", ""),
            "standalone": candidate.get("standalone", ""),
            "visual_only": candidate.get("visual_only", ""),
            "filler_chatter_setup_risk": candidate.get("filler_chatter_setup_risk", ""),
            "local_recommendation": local_recommendation,
            "caption_quote_verified": quote_verified,
        },
    )


def _candidate_from_payload(run: AnalysisRun, payload: CandidatePayload, rank: int) -> CandidateWindow:
    scores = payload.scores.model_dump()
    review_status = "needs_verification" if payload.risks else "pending_review"
    return CandidateWindow(
        stream_id=run.stream_id,
        analysis_run_id=run.analysis_run_id,
        candidate_rank=rank,
        start_seconds=payload.start_seconds,
        end_seconds=payload.end_seconds,
        start_timestamp=payload.start_timestamp,
        end_timestamp=payload.end_timestamp,
        duration_seconds=payload.duration_seconds,
        title=payload.title,
        concise_summary=payload.concise_summary,
        selection_reason=payload.selection_reason,
        primary_pillar=payload.primary_pillar,
        secondary_pillars=list(payload.secondary_pillars),
        tags=normalize_tags(payload.tags),
        transcript_excerpt=payload.transcript_excerpt,
        visual_description=payload.visual_description,
        transcript_evidence=[evidence.model_dump() for evidence in payload.transcript_evidence],
        visual_evidence=[evidence.model_dump() for evidence in payload.visual_evidence],
        contextual_notes=payload.contextual_notes,
        estimated_short_count=payload.estimated_short_count,
        possible_hooks=payload.possible_hooks,
        editing_notes=payload.editing_notes,
        risks=payload.risks,
        scores=scores,
        confidence=scores["confidence"],
        emergent_observations=payload.emergent_observations,
        weighted_score=weighted_score(scores),
        review_status=review_status,
        processing_status="complete",
    )


def _find_duplicate(db: Session, stream: Stream, payload: CandidatePayload, model: str) -> CandidateWindow | None:
    return db.scalar(
        select(CandidateWindow)
        .join(AnalysisRun)
        .where(
            CandidateWindow.stream_id == stream.stream_id,
            CandidateWindow.title == payload.title,
            CandidateWindow.start_seconds == payload.start_seconds,
            CandidateWindow.end_seconds == payload.end_seconds,
            CandidateWindow.review_status != "archived",
            AnalysisRun.model == model,
        )
    )


def _parse_range(value: str) -> tuple[int, int]:
    start, end = [part.strip() for part in value.split("-", 1)]
    return timestamp_to_seconds(start), timestamp_to_seconds(end)


def _adjust_start_to_caption_quote(video_id: str, quote: str, start_seconds: int, end_seconds: int) -> tuple[int, str, bool]:
    if not quote:
        return start_seconds, "", False
    quote_start = _find_quote_start(video_id, quote)
    if quote_start is None:
        return start_seconds, "", False
    if start_seconds <= quote_start <= end_seconds:
        return start_seconds, "caption_quote_verified", True
    if start_seconds - 15 <= quote_start < start_seconds:
        return quote_start, "start_adjusted_to_caption_quote", True
    return start_seconds, "caption_quote_outside_window", False


def _find_quote_start(video_id: str, quote: str) -> int | None:
    caption_path = ROOT_DIR / "data" / "captions" / f"{video_id}.en-orig.json3"
    if not caption_path.exists():
        return None
    data = json.loads(caption_path.read_text(encoding="utf-8"))
    needle_words = _normalized_words(quote).split()
    if not needle_words:
        return None
    words: list[tuple[str, int]] = []
    for event in data.get("events", []):
        seconds = int(event.get("tStartMs", 0) / 1000)
        text = " ".join(segment.get("utf8", "") for segment in event.get("segs", []))
        words.extend((word, seconds) for word in _normalized_words(text).split())
    minimum_match = min(len(needle_words), 8)
    for index in range(0, max(0, len(words) - minimum_match + 1)):
        if [word for word, _ in words[index : index + minimum_match]] == needle_words[:minimum_match]:
            return words[index][1]
    if len(needle_words) > 8:
        shorter_match = 5
        for index in range(0, max(0, len(words) - shorter_match + 1)):
            if [word for word, _ in words[index : index + shorter_match]] == needle_words[:shorter_match]:
                return words[index][1]
    return None


def _normalized_words(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value.lower())).strip()


def _contextual_notes(candidate: dict[str, Any], quote_note: str, quote_verified: bool) -> str:
    parts = [
        "Imported from structured native YouTube Ask pass.",
        f"Local recommendation: {candidate.get('local_recommendation', 'unknown')}",
        f"Complete thought: {candidate.get('complete_thought', '')}",
        f"Payoff inside window: {candidate.get('payoff_inside_window', '')}",
        f"Exact quote inside window: {candidate.get('exact_quote_inside_window', '')}",
        f"Standalone: {candidate.get('standalone', '')}",
        f"Visual only: {candidate.get('visual_only', '')}",
        f"Filler/chatter/setup risk: {candidate.get('filler_chatter_setup_risk', '')}",
        f"Caption quote verified locally: {'yes' if quote_verified else 'no'}",
    ]
    if quote_note:
        parts.append(f"Caption check: {quote_note}")
    return "\n".join(parts)


def _scores(candidate: dict[str, Any], score: int) -> dict[str, int]:
    chatter = str(candidate.get("chatter_risk", "")).lower()
    standalone = str(candidate.get("standalone", "")).lower().startswith("yes")
    complete = str(candidate.get("complete_thought", "")).lower().startswith("yes")
    return {
        "pillar_relevance": score,
        "hook_strength": min(100, score + 2),
        "standalone_clarity": score if standalone and complete else min(score, 70),
        "visual_quality": score,
        "audio_clarity": 78 if "medium" in chatter else 88,
        "emotional_impact": score if _pillar(candidate) in {"personal_journey_recovery", "motivational_inspirational"} else 72,
        "educational_value": score if _pillar(candidate) == "explanation_education" else 72,
        "entertainment_value": min(100, score + 1),
        "editing_potential": score,
        "brand_fit": min(100, score + 2),
        "confidence": 82 if candidate.get("local_flags") else 92,
    }


def _score(candidate: dict[str, Any]) -> int:
    match = re.search(r"\d+", str(candidate.get("score", "")))
    return max(0, min(100, int(match.group(0)))) if match else 80


def _pillar(candidate: dict[str, Any]) -> str:
    text = " ".join(
        str(candidate.get(key, ""))
        for key in ["title", "summary", "why_it_beats_alternatives", "hook", "payoff"]
    ).lower()
    if any(word in text for word in ["homeless", "sobriety", "addiction", "depression", "journey", "recovery"]):
        return "personal_journey_recovery"
    if any(word in text for word in ["demo", "how", "explain", "teaches", "educational"]):
        return "explanation_education"
    if any(word in text for word in ["clapback", "funny", "humor", "personality"]):
        return "humor_personality"
    if any(word in text for word in ["fail", "risk", "nervous", "mistake"]):
        return "mistakes_problem_solving"
    if any(word in text for word in ["art", "purpose", "growth", "motivat"]):
        return "motivational_inspirational"
    return "artistic_process"


def _tags(primary_pillar: str, window_type: str, candidate: dict[str, Any]) -> list[str]:
    tags = [primary_pillar, window_type, STRUCTURED_MODEL]
    if candidate.get("local_recommendation"):
        tags.append(str(candidate["local_recommendation"]))
    return normalize_tags(tags)


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", str(value)).strip()
