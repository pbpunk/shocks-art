import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import ROOT_DIR, get_settings
from app.models import AnalysisRun, CandidateWindow, Stream
from app.schemas.candidate import CandidatePayload, CandidateResponse, seconds_to_timestamp
from app.services.ranking import weighted_score
from app.services.tags import normalize_tags


NATIVE_YOUTUBE_PROMPT_VERSION = "native-youtube-ask-1.3"
NATIVE_YOUTUBE_MODEL = "native-youtube-gemini-sidebar"


@dataclass(frozen=True)
class NativeImportResult:
    run: AnalysisRun
    candidates: list[CandidateWindow]
    skipped_duplicates: int


def native_response_path(run_id: str) -> Path:
    path = ROOT_DIR / "data" / "native_youtube_responses"
    path.mkdir(parents=True, exist_ok=True)
    return path / f"{run_id}.txt"


def build_native_youtube_prompt(stream: Stream) -> str:
    settings = get_settings()
    return f"""Please make a timestamped review guide for this video.

Use only what you can see and hear in the video. Identify up to {settings.candidates_per_stream} distinct, high-quality sections that would be useful for a human reviewer to inspect later.

Do not pick the first acceptable moment by default. Check the beginning, middle, and end of the video and choose the clearest complete sections.

A selected section must contain a complete thought or complete editorial beat. It needs enough setup, development, and resolution that a reviewer can understand why the moment matters without needing a different part of the stream. Do not select a window that only alludes to a story, memory, problem, or lesson unless the same window also explains it clearly enough to stand alone.

For each section, return this exact format:

1. Section Title (MM:SS - MM:SS)
Rank:
Duration:
Primary Pillar:
Summary:
Why It Is Useful:
Tags:
Transcript Evidence:
Visual Evidence:
Completeness Check:
Window Type:
Chatter Risk:
Exact Caption Quote:
Estimated Short Count:
Possible Opening Lines:
Usefulness Score:
Component Scores: Pillar: 0-100, Hook: 0-100, Clarity: 0-100, Visuals: 0-100, Audio: 0-100, Impact: 0-100, Education: 0-100, Entertainment: 0-100, Potential: 0-100, Brand: 0-100, Confidence: 0-100

Review rules:
- Prefer 5-10 minute source windows when available. Shorter sections are okay only when the topic is naturally complete and should be marked Window Type: short_ready.
- Sections should not substantially overlap.
- Favor visible craft process, Nate or hands in frame, clear audio, compelling personal context, humor, problem-solving, or educational explanation.
- Avoid dead air, repetitive greetings, unclear audio, weak visuals, and anything that would be misleading without context.
- Reject or score below 70 any section where the strongest moment is only a teaser, tangent, unfinished thought, dangling allusion, or contextless reference.
- Personal-story sections must include the actual story or lesson, not just a passing mention of addiction, homelessness, recovery, business growth, or hardship.
- Process sections must include a coherent task with visible before/action/after or a clear explanation of what changed.
- In Completeness Check, state the section's beginning, middle, and end in one sentence. If there is no clear end or payoff, do not choose that section.
- Window Type must be one of: source_window, short_ready, needs_trim, reject.
- Chatter Risk must be one of: low, medium, high. Mark high when more than about one third of the window is greetings, unrelated Q&A, tool searching, setup fumbling, or topic drift.
- Exact Caption Quote must be a short exact phrase from inside the chosen timestamps. Do not use ellipses in Exact Caption Quote. Do not paraphrase it.
- Transcript evidence must be inside the selected section and include timestamps.
- Visual evidence must be inside the selected section and include timestamps or a time range.
- If you cannot verify a quote inside the selected window, say so instead of guessing.

Score calibration:
- 90-100: obvious keeper; complete beat, strong opening, useful visuals/audio through most of the window, low chatter.
- 80-89: probably usable; complete but may need light trimming or has one moderate weakness.
- 70-79: maybe; needs verification, trimming, or has uneven visuals/chatter.
- Below 70: do not choose unless the video has no better complete sections.
- Confidence above 90 requires exact in-window evidence and low uncertainty. Do not give 90+ confidence to paraphrased quotes or loose timestamp guesses.

Video metadata for reference:
Title: {stream.title}
URL: {stream.url}
Source video ID: {stream.source_video_id}
"""


def build_native_youtube_fallback_prompt(stream: Stream) -> str:
    settings = get_settings()
    return f"""Please summarize the best {settings.candidates_per_stream} timestamped sections in this video for a human review log.

Use only the video. Include exact start and end times.

Format each section like this:

1. Title (MM:SS - MM:SS)
Rank:
Duration:
Primary Pillar:
Summary:
Selection Reason:
Tags:
Transcript Evidence:
Visual Evidence:
Completeness Check:
Window Type:
Chatter Risk:
Exact Caption Quote:
Estimated Short Count:
Possible Hooks:
Editing Potential:
Component Scores: Pillar: 0-100, Hook: 0-100, Clarity: 0-100, Visuals: 0-100, Audio: 0-100, Impact: 0-100, Education: 0-100, Entertainment: 0-100, Potential: 0-100, Brand: 0-100, Confidence: 0-100

- Each section must contain a complete thought with setup, development, and payoff.
- Do not select a section that only hints at a larger story without resolving it inside the selected timestamps.
- Include an exact short caption quote without ellipses.
- Use stricter scoring: 90+ only for obvious keepers, 70s for maybes.

Video:
{stream.url}
"""


def parse_native_youtube_response(text: str, stream: Stream) -> CandidateResponse:
    blocks = _candidate_blocks(text)
    if not blocks:
        raise ValueError("No numbered candidate blocks with timestamp ranges were found.")
    payloads = [_parse_candidate_block(block, index + 1) for index, block in enumerate(blocks)]
    return CandidateResponse(
        schema_version="1.0",
        stream_id=stream.stream_id,
        source_video_id=stream.source_video_id,
        candidates=payloads,
    )


def save_native_youtube_response(
    db: Session,
    stream: Stream,
    response_text: str,
    source: str = NATIVE_YOUTUBE_MODEL,
) -> NativeImportResult:
    run = AnalysisRun(
        stream_id=stream.stream_id,
        model=source,
        prompt_version=NATIVE_YOUTUBE_PROMPT_VERSION,
        schema_version="1.0",
        status="processing",
        request_started_at=datetime.now(timezone.utc),
    )
    db.add(run)
    db.flush()

    raw_path = native_response_path(run.analysis_run_id)
    raw_path.write_text(response_text, encoding="utf-8")
    run.raw_response_location = str(raw_path)

    try:
        response = parse_native_youtube_response(response_text, stream)
        candidates: list[CandidateWindow] = []
        skipped_duplicates = 0
        for rank, payload in enumerate(response.candidates, start=1):
            existing = _find_duplicate(db, stream, payload)
            if existing:
                skipped_duplicates += 1
                continue
            candidate = _candidate_from_payload(run, payload, rank)
            candidates.append(candidate)

        db.add_all(candidates)
        run.status = "complete"
        run.request_completed_at = datetime.now(timezone.utc)
        run.exception_message = (
            f"Imported {len(candidates)} candidate(s); skipped {skipped_duplicates} duplicate(s)."
            if skipped_duplicates
            else ""
        )
        stream.processing_status = "complete"
        db.flush()
        return NativeImportResult(run=run, candidates=candidates, skipped_duplicates=skipped_duplicates)
    except Exception as exc:
        run.status = "failed"
        run.request_completed_at = datetime.now(timezone.utc)
        run.exception_message = str(exc)
        run.validation_errors = [str(exc)]
        db.flush()
        raise


def _candidate_blocks(text: str) -> list[str]:
    pattern = re.compile(
        r"(?ms)^\s*(?:\d+\.\s+)?[^\n()]+?\(\s*\d{1,2}:\d{2}(?::\d{2})?\s*-\s*\d{1,2}:\d{2}(?::\d{2})?\s*\)\s*\n\s*Rank\s*:.*?(?=^\s*(?:\d+\.\s+)?[^\n()]+?\(\s*\d{1,2}:\d{2}(?::\d{2})?\s*-\s*\d{1,2}:\d{2}(?::\d{2})?\s*\)\s*\n\s*Rank\s*:|\Z)"
    )
    return [match.group(0).strip() for match in pattern.finditer(text)]


def _parse_candidate_block(block: str, fallback_rank: int) -> CandidatePayload:
    header = re.match(
        r"(?s)^\s*(?:(?P<number>\d+)\.\s+)?(?P<title>.+?)\s*\(\s*(?P<start>\d{1,2}:\d{2}(?::\d{2})?)\s*-\s*(?P<end>\d{1,2}:\d{2}(?::\d{2})?)\s*\)",
        block,
    )
    if not header:
        raise ValueError(f"Candidate {fallback_rank} is missing a numbered title timestamp header.")

    start_seconds = timestamp_to_seconds(header.group("start"))
    end_seconds = timestamp_to_seconds(header.group("end"))
    scores = _parse_scores(_field(block, "Component Scores")) | {
        "editing_potential": _parse_int(_field(block, "Editing Potential") or _field(block, "Usefulness Score"), default=80),
    }
    scores = _complete_scores(scores)

    transcript_text = _field(block, "Transcript Evidence")
    visual_text = _field(block, "Visual Evidence")
    completeness_check = _field(block, "Completeness Check")
    window_type = _normalize_window_type(_field(block, "Window Type"), end_seconds - start_seconds)
    chatter_risk = _normalize_chatter_risk(_field(block, "Chatter Risk"))
    exact_caption_quote = _field(block, "Exact Caption Quote")
    scores = _calibrate_scores(scores, window_type, chatter_risk, exact_caption_quote, completeness_check)
    return CandidatePayload(
        title=_clean_text(header.group("title")),
        start_seconds=start_seconds,
        end_seconds=end_seconds,
        start_timestamp=seconds_to_timestamp(start_seconds),
        end_timestamp=seconds_to_timestamp(end_seconds),
        duration_seconds=end_seconds - start_seconds,
        concise_summary=_field(block, "Summary") or "Imported from native YouTube Ask response.",
        selection_reason=_field(block, "Selection Reason") or _field(block, "Why It Is Useful") or "Selected by native YouTube Ask response.",
        primary_pillar=_normalize_pillar(_field(block, "Primary Pillar")),
        secondary_pillars=[],
        tags=normalize_tags(_parse_tags(_field(block, "Tags"))),
        transcript_excerpt=transcript_text,
        visual_description=visual_text,
        transcript_evidence=_parse_evidence(transcript_text, start_seconds, end_seconds),
        visual_evidence=_parse_evidence(visual_text, start_seconds, end_seconds),
        contextual_notes=_contextual_notes(completeness_check, window_type, chatter_risk, exact_caption_quote),
        estimated_short_count=max(1, _parse_int(_field(block, "Estimated Short Count"), default=1)),
        possible_hooks=_parse_hooks(_field(block, "Possible Hooks") or _field(block, "Possible Opening Lines")),
        editing_notes=[],
        risks=_candidate_risks(window_type, chatter_risk, exact_caption_quote, scores),
        scores=scores,
        emergent_observations={
            "source": NATIVE_YOUTUBE_MODEL,
            "rank": _parse_int(_field(block, "Rank"), default=fallback_rank),
            "raw_duration": _field(block, "Duration"),
            "window_type": window_type,
            "chatter_risk": chatter_risk,
            "exact_caption_quote": exact_caption_quote,
            "native_prompt_version": NATIVE_YOUTUBE_PROMPT_VERSION,
        },
    )


def _candidate_from_payload(run: AnalysisRun, payload: CandidatePayload, rank: int) -> CandidateWindow:
    scores = payload.scores.model_dump()
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
        review_status="pending_review",
        processing_status="complete",
    )


def _find_duplicate(db: Session, stream: Stream, payload: CandidatePayload) -> CandidateWindow | None:
    return db.scalar(
        select(CandidateWindow).where(
            CandidateWindow.stream_id == stream.stream_id,
            CandidateWindow.title == payload.title,
            CandidateWindow.start_seconds == payload.start_seconds,
            CandidateWindow.end_seconds == payload.end_seconds,
        )
    )


def _field(block: str, label: str) -> str:
    labels = [
        "Rank",
        "Duration",
        "Primary Pillar",
        "Summary",
        "Selection Reason",
        "Why It Is Useful",
        "Tags",
        "Transcript Evidence",
        "Visual Evidence",
        "Completeness Check",
        "Window Type",
        "Chatter Risk",
        "Exact Caption Quote",
        "Estimated Short Count",
        "Possible Hooks",
        "Possible Opening Lines",
        "Editing Potential",
        "Usefulness Score",
        "Component Scores",
    ]
    label_pattern = "|".join(re.escape(item) for item in labels)
    match = re.search(
        rf"(?ms)^\s*{re.escape(label)}\s*:\s*(.*?)(?=^\s*(?:{label_pattern})\s*:|\Z)",
        block,
    )
    return _clean_text(match.group(1)) if match else ""


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" -\n\t")


def timestamp_to_seconds(value: str) -> int:
    parts = [int(part) for part in value.strip().split(":")]
    if len(parts) == 2:
        minutes, seconds = parts
        return minutes * 60 + seconds
    if len(parts) == 3:
        hours, minutes, seconds = parts
        return hours * 3600 + minutes * 60 + seconds
    raise ValueError(f"Invalid timestamp: {value}")


def _parse_tags(value: str) -> list[str]:
    return [tag.strip().lstrip("#") for tag in re.split(r"[,#]", value) if tag.strip()]


def _contextual_notes(completeness_check: str, window_type: str = "", chatter_risk: str = "", exact_caption_quote: str = "") -> str:
    note = "Imported from YouTube's native Ask/Gemini sidebar. Verify in review before final editing."
    parts = []
    if completeness_check:
        parts.append(f"Completeness check: {completeness_check}")
    if window_type:
        parts.append(f"Window type: {window_type}")
    if chatter_risk:
        parts.append(f"Chatter risk: {chatter_risk}")
    if exact_caption_quote:
        parts.append(f"Exact caption quote: {exact_caption_quote}")
    if parts:
        return "\n".join(parts) + f"\n\n{note}"
    return note


def _parse_hooks(value: str) -> list[str]:
    if not value:
        return []
    quoted = re.findall(r'"([^"]+)"', value)
    if quoted:
        return quoted
    return [_clean_text(item) for item in re.split(r"\s*/\s*|\s+\|\s+", value) if _clean_text(item)]


def _parse_evidence(value: str, start_seconds: int, end_seconds: int) -> list[dict]:
    if not value or "no verified" in value.lower():
        return []
    timestamps = re.findall(r"\(?(\d{1,2}:\d{2}(?::\d{2})?)\)?", value)
    evidence = []
    for timestamp in timestamps[:3]:
        seconds = timestamp_to_seconds(timestamp)
        if start_seconds <= seconds <= end_seconds:
            evidence.append(
                {
                    "timestamp": seconds_to_timestamp(seconds),
                    "seconds": seconds,
                    "text": value,
                }
            )
    if evidence:
        return evidence
    return [{"timestamp": seconds_to_timestamp(start_seconds), "seconds": start_seconds, "text": value}]


def _normalize_window_type(value: str, duration_seconds: int) -> str:
    normalized = value.lower().replace("-", "_").replace(" ", "_")
    for option in ["source_window", "short_ready", "needs_trim", "reject"]:
        if option in normalized:
            return option
    if duration_seconds < 120:
        return "short_ready"
    return "source_window"


def _normalize_chatter_risk(value: str) -> str:
    normalized = value.lower()
    if "high" in normalized:
        return "high"
    if "medium" in normalized or "moderate" in normalized:
        return "medium"
    if "low" in normalized:
        return "low"
    return "medium"


def _calibrate_scores(
    scores: dict[str, int],
    window_type: str,
    chatter_risk: str,
    exact_caption_quote: str,
    completeness_check: str,
) -> dict[str, int]:
    calibrated = dict(scores)
    caps: list[int] = []
    if window_type == "reject":
        caps.append(59)
    elif window_type == "needs_trim":
        caps.append(82)
    elif window_type == "short_ready":
        caps.append(84)
    if chatter_risk == "high":
        caps.append(76)
    elif chatter_risk == "medium":
        caps.append(88)
    if not exact_caption_quote:
        caps.append(84)
    elif "..." in exact_caption_quote or "…" in exact_caption_quote:
        caps.append(82)
    if not completeness_check:
        caps.append(86)

    if caps:
        cap = min(caps)
        for key in [
            "hook_strength",
            "standalone_clarity",
            "emotional_impact",
            "educational_value",
            "entertainment_value",
            "editing_potential",
            "brand_fit",
            "confidence",
        ]:
            calibrated[key] = min(calibrated.get(key, cap), cap)
    if chatter_risk == "high":
        calibrated["standalone_clarity"] = min(calibrated["standalone_clarity"], 72)
        calibrated["editing_potential"] = min(calibrated["editing_potential"], 72)
    return calibrated


def _candidate_risks(window_type: str, chatter_risk: str, exact_caption_quote: str, scores: dict[str, int]) -> list[str]:
    risks = []
    if window_type == "needs_trim":
        risks.append("needs_trim")
    elif window_type == "short_ready":
        risks.append("short_ready_not_source_window")
    elif window_type == "reject":
        risks.append("native_model_marked_reject")
    if chatter_risk in {"medium", "high"}:
        risks.append(f"{chatter_risk}_chatter_risk")
    if not exact_caption_quote:
        risks.append("missing_exact_caption_quote")
    elif "..." in exact_caption_quote or "…" in exact_caption_quote:
        risks.append("caption_quote_contains_ellipsis")
    if scores.get("confidence", 0) < 85:
        risks.append("needs_manual_verification")
    return risks


def _normalize_pillar(value: str) -> str:
    normalized = value.lower().replace("&", "and")
    mapping = {
        "motiv": "motivational_inspirational",
        "inspir": "motivational_inspirational",
        "personal": "personal_journey_recovery",
        "recovery": "personal_journey_recovery",
        "addiction": "personal_journey_recovery",
        "journey": "personal_journey_recovery",
        "artistic": "artistic_process",
        "process": "artistic_process",
        "wood": "artistic_process",
        "epoxy": "artistic_process",
        "explanation": "explanation_education",
        "education": "explanation_education",
        "advice": "explanation_education",
        "humor": "humor_personality",
        "personality": "humor_personality",
        "mistake": "mistakes_problem_solving",
        "problem": "mistakes_problem_solving",
    }
    for needle, pillar in mapping.items():
        if needle in normalized:
            return pillar
    return "emergent_miscellaneous"


def _parse_scores(value: str) -> dict[str, int]:
    aliases = {
        "pillar": "pillar_relevance",
        "hook": "hook_strength",
        "clarity": "standalone_clarity",
        "visuals": "visual_quality",
        "audio": "audio_clarity",
        "impact": "emotional_impact",
        "education": "educational_value",
        "entertainment": "entertainment_value",
        "potential": "editing_potential",
        "brand": "brand_fit",
        "confidence": "confidence",
    }
    scores: dict[str, int] = {}
    for name, raw_score in re.findall(r"([A-Za-z ]+):\s*(\d{1,3})", value):
        key = aliases.get(name.strip().lower())
        if key:
            scores[key] = min(100, max(0, int(raw_score)))
    return scores


def _complete_scores(scores: dict[str, int]) -> dict[str, int]:
    defaults = {
        "pillar_relevance": 80,
        "hook_strength": 75,
        "standalone_clarity": 75,
        "visual_quality": 75,
        "audio_clarity": 75,
        "emotional_impact": 70,
        "educational_value": 70,
        "entertainment_value": 70,
        "editing_potential": 80,
        "brand_fit": 80,
        "confidence": 70,
    }
    return defaults | scores


def _parse_int(value: str, default: int) -> int:
    match = re.search(r"\d+", value or "")
    return int(match.group(0)) if match else default
