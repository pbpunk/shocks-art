from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models import AnalysisRun, CandidateWindow, Stream
from app.services.native_automation import (
    read_native_job_status,
    run_prompt_attempts,
    write_native_job_status,
)
from app.services.native_youtube import (
    NATIVE_YOUTUBE_MODEL,
    NATIVE_YOUTUBE_PROMPT_VERSION,
    _candidate_from_payload,
    build_native_youtube_fallback_prompt,
    build_native_youtube_prompt,
    native_response_path,
    parse_native_youtube_response,
)


NATIVE_ASK_MODEL_PREFIX = NATIVE_YOUTUBE_MODEL
CLIPS_NATIVE_ASK_SOURCE = f"{NATIVE_ASK_MODEL_PREFIX}-clips-update"


def is_native_ask_model(model: str | None) -> bool:
    return str(model or "").startswith(NATIVE_ASK_MODEL_PREFIX)


def pending_native_ask_streams(db: Session) -> list[Stream]:
    """Return streams that have not completed the production YouTube Ask workflow.

    Stream.processing_status is intentionally not authoritative here. Legacy direct
    Gemini analysis marked streams complete even though those runs are not valid Clips
    production inputs. A completed native YouTube Ask AnalysisRun is the durable proof
    that a stream has passed the production editorial path.
    """

    completed_native_run = exists().where(
        AnalysisRun.stream_id == Stream.stream_id,
        AnalysisRun.status == "complete",
        AnalysisRun.model.like(f"{NATIVE_ASK_MODEL_PREFIX}%"),
    )
    return list(
        db.scalars(
            select(Stream)
            .where(~completed_native_run)
            .order_by(Stream.published_at.desc())
        ).all()
    )


def production_clip_candidates(
    db: Session,
    limit: int,
    include_check: bool,
) -> tuple[list[CandidateWindow], int]:
    """Return only candidates whose lineage comes from native YouTube Ask."""

    query = (
        select(CandidateWindow)
        .join(AnalysisRun)
        .where(
            CandidateWindow.review_status != "archived",
            AnalysisRun.model.like(f"{NATIVE_ASK_MODEL_PREFIX}%"),
        )
    )
    if not include_check:
        query = query.where(CandidateWindow.review_status != "needs_verification")
    candidates = list(db.scalars(query).all())
    candidates.sort(key=lambda candidate: (candidate.stream.published_at, candidate.weighted_score), reverse=True)
    return candidates[:limit], len(candidates)


def production_candidate_ids(db: Session) -> set[str]:
    return set(
        db.scalars(
            select(CandidateWindow.candidate_window_id)
            .join(AnalysisRun)
            .where(AnalysisRun.model.like(f"{NATIVE_ASK_MODEL_PREFIX}%"))
        ).all()
    )


def _native_duplicate(db: Session, stream: Stream, payload) -> CandidateWindow | None:
    """Deduplicate only against prior native Ask candidates.

    A legacy direct-Gemini row with the same title/timestamps must never suppress a
    valid replacement imported from YouTube Ask.
    """

    return db.scalar(
        select(CandidateWindow)
        .join(AnalysisRun)
        .where(
            CandidateWindow.stream_id == stream.stream_id,
            CandidateWindow.title == payload.title,
            CandidateWindow.start_seconds == payload.start_seconds,
            CandidateWindow.end_seconds == payload.end_seconds,
            AnalysisRun.model.like(f"{NATIVE_ASK_MODEL_PREFIX}%"),
        )
    )


def save_clips_native_ask_response(db: Session, stream: Stream, response_text: str):
    """Persist a YouTube Ask response as the only production Clips candidate source."""

    run = AnalysisRun(
        stream_id=stream.stream_id,
        model=CLIPS_NATIVE_ASK_SOURCE,
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
            if _native_duplicate(db, stream, payload):
                skipped_duplicates += 1
                continue
            candidates.append(_candidate_from_payload(run, payload, rank))

        db.add_all(candidates)
        run.status = "complete"
        run.request_completed_at = datetime.now(timezone.utc)
        run.exception_message = (
            f"Imported {len(candidates)} candidate(s); skipped {skipped_duplicates} native duplicate(s)."
            if skipped_duplicates
            else ""
        )
        stream.processing_status = "complete"
        db.flush()
        return run, candidates, skipped_duplicates
    except Exception as exc:
        run.status = "failed"
        run.request_completed_at = datetime.now(timezone.utc)
        run.exception_message = str(exc)
        run.validation_errors = [str(exc)]
        stream.processing_status = "failed"
        db.flush()
        raise


def run_clips_native_ask(stream_id: str) -> dict:
    """Run the production Clips analysis through YouTube's native Ask UI.

    This function never calls the Gemini API. It delegates interpretation to the
    existing browser automation that opens the YouTube video and uses the page's Ask
    interaction, then imports that response locally.
    """

    write_native_job_status(
        stream_id,
        stream_id=stream_id,
        status="starting",
        message="Preparing YouTube Ask for Clips update.",
        started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        source=CLIPS_NATIVE_ASK_SOURCE,
    )

    with SessionLocal() as db:
        stream = db.get(Stream, stream_id)
        if not stream:
            return write_native_job_status(
                stream_id,
                status="failed",
                message=f"Stream not found: {stream_id}",
            )
        url = stream.url
        primary_prompt = build_native_youtube_prompt(stream)
        fallback_prompt = build_native_youtube_fallback_prompt(stream)

    output_path = run_prompt_attempts(stream_id, url, primary_prompt, fallback_prompt)
    if not output_path:
        with SessionLocal() as db:
            stream = db.get(Stream, stream_id)
            if stream:
                stream.processing_status = "failed"
                db.commit()
        return read_native_job_status(stream_id)

    with SessionLocal() as db:
        stream = db.get(Stream, stream_id)
        if not stream:
            return write_native_job_status(
                stream_id,
                status="failed",
                message=f"Stream disappeared before Ask import: {stream_id}",
            )
        try:
            run, candidates, skipped_duplicates = save_clips_native_ask_response(
                db,
                stream,
                output_path.read_text(encoding="utf-8"),
            )
            db.commit()
            return write_native_job_status(
                stream_id,
                status="complete",
                message=(
                    f"Imported {len(candidates)} YouTube Ask candidate(s); "
                    f"skipped {skipped_duplicates} native duplicate(s)."
                ),
                analysis_run_id=run.analysis_run_id,
                candidate_window_ids=[candidate.candidate_window_id for candidate in candidates],
                source=CLIPS_NATIVE_ASK_SOURCE,
            )
        except Exception as exc:
            db.commit()
            return write_native_job_status(
                stream_id,
                status="failed",
                message=f"YouTube Ask import failed: {exc}",
                source=CLIPS_NATIVE_ASK_SOURCE,
            )
