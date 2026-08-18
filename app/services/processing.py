import time
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.indexing.stream_media import sync_stream_media
from app.models import AnalysisRun, CandidateWindow, Stream
from app.services.gemini import GeminiAnalyzer, debug_log_path, raw_response_path
from app.services.repository import create_analysis_run, save_candidates, upsert_stream
from app.services.stream_archive import ensure_stream_transcript, try_ensure_stream_transcript
from app.services.validation import CandidateValidationError, validate_candidate_response
from app.services.youtube import YouTubeClient


def summarize_exception(exc: Exception) -> str:
    message = str(exc).replace("\n", " ").strip()
    if "RESOURCE_EXHAUSTED" in message or "Quota exceeded" in message:
        return "Gemini quota exceeded for the configured model. Switch to a Flash model or enable billing/quota for the selected Gemini model."
    if "no longer available to new users" in message or "'code': 'not_found'" in message or "NOT_FOUND" in message:
        return "The configured Gemini model is not available for this API project. Choose an available model, such as gemini-3.1-flash-lite, and retry extraction."
    if "UNAVAILABLE" in message or "high demand" in message:
        return "The configured Gemini model is temporarily unavailable or overloaded. Retry later or switch to another available model."
    if "GEMINI_API_KEY" in message:
        return "Gemini API key is missing. Add GEMINI_API_KEY to .env and restart the app."
    if "YOUTUBE_API_KEY" in message:
        return "YouTube API key is missing. Add YOUTUBE_API_KEY to .env and restart the app."
    return message[:1000] if message else exc.__class__.__name__


def append_debug_log(run: AnalysisRun, heading: str, body: str = "") -> None:
    path = debug_log_path(run.analysis_run_id)
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"\n===== {timestamp} {heading} =====\n")
        if body:
            handle.write(body.rstrip() + "\n")


def discover_and_store_streams(db: Session, youtube_client: YouTubeClient | None = None) -> dict:
    settings = get_settings()
    client = youtube_client or YouTubeClient(settings.youtube_api_key)
    discovered = client.discover_streams(settings.youtube_channel_handle)
    created = 0
    updated = 0
    transcripts_available = 0
    transcripts_missing = 0
    library_media_created = 0
    library_media_updated = 0
    language_traces_created = 0
    language_traces_reused = 0
    library_sync_errors = 0
    for stream_data in discovered:
        stream, was_created = upsert_stream(db, stream_data)
        created += int(was_created)
        updated += int(not was_created)
        transcript = try_ensure_stream_transcript(db, stream, fetch_missing=True)
        if transcript:
            transcripts_available += 1
        else:
            transcripts_missing += 1
        try:
            _, media_created, trace_created, trace_reused, _ = sync_stream_media(
                db,
                stream,
                import_language=True,
            )
            library_media_created += int(media_created)
            library_media_updated += int(not media_created)
            language_traces_created += trace_created
            language_traces_reused += trace_reused
        except Exception:
            db.rollback()
            library_sync_errors += 1
    db.commit()
    return {
        "discovered": len(discovered),
        "created": created,
        "updated": updated,
        "transcripts_available": transcripts_available,
        "transcripts_missing": transcripts_missing,
        "library_media_created": library_media_created,
        "library_media_updated": library_media_updated,
        "language_traces_created": language_traces_created,
        "language_traces_reused": language_traces_reused,
        "library_sync_errors": library_sync_errors,
    }


def analyze_one_stream(db: Session, stream_id: str, analyzer: GeminiAnalyzer | None = None) -> list[CandidateWindow]:
    settings = get_settings()
    stream = db.get(Stream, stream_id)
    if not stream:
        raise ValueError(f"Stream not found: {stream_id}")
    existing = list(db.scalars(
        select(CandidateWindow).where(
            CandidateWindow.stream_id == stream_id,
            CandidateWindow.processing_status == "complete",
        ).order_by(CandidateWindow.candidate_rank)
    ).all())
    if existing:
        return existing

    run = create_analysis_run(db, stream, settings.gemini_model, settings.prompt_version, settings.schema_version)
    db.commit()
    return analyze_existing_run(db, run.analysis_run_id, analyzer=analyzer)


def analyze_existing_run(db: Session, run_id: str, analyzer: GeminiAnalyzer | None = None) -> list[CandidateWindow]:
    settings = get_settings()
    run = db.get(AnalysisRun, run_id)
    if not run:
        raise ValueError(f"Analysis run not found: {run_id}")
    stream = run.stream
    active_analyzer = analyzer or GeminiAnalyzer(settings.gemini_api_key, run.model, run.schema_version)
    try:
        append_debug_log(
            run,
            "RUN START",
            f"model: {run.model}\nstream: {stream.title}\nurl: {stream.url}\nvideo_id: {stream.source_video_id}",
        )
        transcript = ensure_stream_transcript(db, stream, fetch_missing=True)
        try:
            sync_stream_media(db, stream, import_language=True)
        except Exception:
            db.rollback()
        append_debug_log(
            run,
            "TRANSCRIPT CAPTURE",
            f"status: {'stored' if transcript else 'unavailable'}",
        )
        if hasattr(active_analyzer, "build_analysis_prompt"):
            append_debug_log(run, "PROMPT TO GEMINI", active_analyzer.build_analysis_prompt(stream))
    except Exception:
        pass
    raw = ""
    errors: list[str] = []
    try:
        for attempt in range(settings.max_retries + 1):
            run.retry_count = attempt
            run.status = "processing"
            stream.processing_status = "processing"
            db.commit()
            append_debug_log(run, f"ATTEMPT {attempt}")
            if attempt == 0:
                raw = active_analyzer.analyze_stream(stream)
            else:
                if hasattr(active_analyzer, "build_repair_prompt"):
                    append_debug_log(run, "REPAIR PROMPT TO GEMINI", active_analyzer.build_repair_prompt(raw, errors))
                raw = active_analyzer.repair_response(raw, errors)
            append_debug_log(run, f"GEMINI RESPONSE ATTEMPT {attempt}", raw)
            path = raw_response_path(run.analysis_run_id)
            path.write_text(raw, encoding="utf-8")
            run.raw_response_location = str(path)
            try:
                validated = validate_candidate_response(raw, settings.schema_version)
                candidates = save_candidates(db, run, validated)
                run.request_completed_at = datetime.now(timezone.utc)
                append_debug_log(run, "VALIDATION OK", f"saved_candidates: {len(candidates)}")
                db.commit()
                return candidates
            except CandidateValidationError as exc:
                errors = exc.errors
                run.validation_errors = errors
                run.exception_message = "Validation failed: " + "; ".join(errors[:5])
                append_debug_log(run, "VALIDATION FAILED", "\n".join(errors))
                db.commit()
                if attempt < settings.max_retries:
                    time.sleep(2**attempt)
        run.status = "quarantined"
        stream.processing_status = "quarantined"
        run.exception_message = "Response could not be repaired: " + "; ".join(errors[:5])
        run.request_completed_at = datetime.now(timezone.utc)
        append_debug_log(run, "QUARANTINED", run.exception_message)
        db.commit()
        raise CandidateValidationError(errors)
    except Exception as exc:
        run.status = "failed" if run.status != "quarantined" else run.status
        stream.processing_status = run.status
        run.exception_message = summarize_exception(exc)
        run.request_completed_at = datetime.now(timezone.utc)
        append_debug_log(run, "FAILED", run.exception_message)
        db.commit()
        raise


def start_next_analysis_run(db: Session) -> AnalysisRun | None:
    settings = get_settings()
    stream = db.scalar(
        select(Stream)
        .where(Stream.processing_status.in_(["queued", "failed"]))
        .order_by(Stream.published_at.desc())
    )
    if not stream:
        return None
    run = create_analysis_run(db, stream, settings.gemini_model, settings.prompt_version, settings.schema_version)
    db.commit()
    return run


def run_analysis_background(run_id: str) -> None:
    with SessionLocal() as db:
        try:
            analyze_existing_run(db, run_id)
        except Exception:
            pass


def process_queued_streams(db: Session, limit: int | None = None) -> dict:
    query = select(Stream).where(Stream.processing_status.in_(["queued", "failed"])).order_by(Stream.published_at.desc())
    streams = list(db.scalars(query).all())
    if limit:
        streams = streams[:limit]
    complete = 0
    failed = 0
    for stream in streams:
        try:
            analyze_one_stream(db, stream.stream_id)
            complete += 1
        except Exception:
            failed += 1
    return {"attempted": len(streams), "complete": complete, "failed": failed}
