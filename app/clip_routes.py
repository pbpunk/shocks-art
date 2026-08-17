from datetime import datetime, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, BackgroundTasks, Depends
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.core.database import SessionLocal, get_db
from app.models import CandidateWindow, Stream
from app.services.clip_new_state import read_new_clip_ids, replace_new_clip_ids
from app.services.clip_update_state import read_clip_update_state, write_clip_update_state
from app.services.processing import analyze_one_stream, discover_and_store_streams, summarize_exception


router = APIRouter()
ACTIVE_UPDATE_STATUSES = {"checking", "processing"}


def url_path(request: Request, path: str) -> str:
    root_path = request.scope.get("app_prefix", request.scope.get("root_path", "")).rstrip("/")
    normalized_path = path if path.startswith("/") else f"/{path}"
    return f"{root_path}{normalized_path}" if root_path else normalized_path


def local_redirect(request: Request, path: str, status_code: int = 303) -> RedirectResponse:
    return RedirectResponse(url=url_path(request, path), status_code=status_code)


def pending_streams(db: Session) -> list[Stream]:
    return list(
        db.scalars(
            select(Stream)
            .where(Stream.processing_status.in_(["queued", "failed"]))
            .order_by(Stream.published_at.desc())
        ).all()
    )


def update_is_recently_active(state: dict) -> bool:
    if state.get("status") not in ACTIVE_UPDATE_STATUSES:
        return False
    try:
        updated_at = datetime.fromisoformat(str(state.get("updated_at", "")))
    except ValueError:
        return False
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - updated_at).total_seconds() < 6 * 60 * 60


def process_stream_queue_background() -> None:
    """Process each waiting stream while publishing durable, truthful progress."""
    with SessionLocal() as db:
        streams = pending_streams(db)
        total = len(streams)
        before_ids = set(db.scalars(select(CandidateWindow.candidate_window_id)).all())
        completed = 0
        failed = 0

        try:
            for index, stream in enumerate(streams, start=1):
                write_clip_update_state(
                    "processing",
                    f"Processing {index}/{total}: {stream.title}",
                    phase="analyzing",
                    total_streams=total,
                    current_index=index,
                    completed_streams=completed,
                    failed_streams=failed,
                    current_stream_id=stream.stream_id,
                    current_stream_title=stream.title,
                )
                try:
                    analyze_one_stream(db, stream.stream_id)
                    completed += 1
                except Exception:
                    failed += 1

                write_clip_update_state(
                    "processing",
                    f"Finished {index}/{total}: {stream.title}",
                    phase="between_streams",
                    total_streams=total,
                    current_index=index,
                    completed_streams=completed,
                    failed_streams=failed,
                    current_stream_id=stream.stream_id,
                    current_stream_title=stream.title,
                )

            after_ids = set(db.scalars(select(CandidateWindow.candidate_window_id)).all())
            new_ids = after_ids - before_ids
            replace_new_clip_ids(new_ids)

            message = f"Update complete: {len(new_ids)} new clip(s) surfaced."
            if failed:
                message += f" {failed} stream(s) failed."
            write_clip_update_state(
                "complete",
                message,
                phase="complete",
                new_clips=len(new_ids),
                total_streams=total,
                current_index=total,
                completed_streams=completed,
                failed_streams=failed,
                current_stream_id=None,
                current_stream_title=None,
            )
        except Exception as exc:
            write_clip_update_state(
                "failed",
                summarize_exception(exc),
                phase="failed",
                total_streams=total,
                current_index=completed + failed,
                completed_streams=completed,
                failed_streams=failed,
            )


def begin_clip_update(background_tasks: BackgroundTasks, db: Session) -> dict:
    existing_state = read_clip_update_state()
    if update_is_recently_active(existing_state):
        return existing_state

    write_clip_update_state("checking", "Checking for new streams…", phase="discovering")
    result = discover_and_store_streams(db)
    streams = pending_streams(db)
    pending_count = len(streams)

    if pending_count:
        state = write_clip_update_state(
            "processing",
            f"Preparing {pending_count} stream(s) for processing…",
            phase="queued",
            total_streams=pending_count,
            current_index=0,
            completed_streams=0,
            failed_streams=0,
            pending_streams=pending_count,
            discovered_streams=result.get("created", 0),
        )
        background_tasks.add_task(process_stream_queue_background)
        return state

    return write_clip_update_state(
        "complete",
        "Up to date — no streams are waiting to process.",
        phase="complete",
        new_clips=0,
        total_streams=0,
        current_index=0,
        completed_streams=0,
        failed_streams=0,
        pending_streams=0,
        discovered_streams=result.get("created", 0),
    )


@router.get("/api/clips/new-state")
def new_clip_state_api():
    return {"candidate_window_ids": sorted(read_new_clip_ids())}


@router.get("/api/clips/update-state")
def clip_update_state_api():
    return read_clip_update_state()


@router.post("/api/clips/update")
def clip_update_api(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    try:
        return begin_clip_update(background_tasks, db)
    except Exception as exc:
        error_message = summarize_exception(exc)
        write_clip_update_state("failed", error_message, phase="failed")
        return {"status": "failed", "message": error_message}


@router.post("/actions/clips/refresh-streams")
def refresh_streams_action(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Compatibility endpoint: start the update, then always return to Clips."""
    try:
        state = begin_clip_update(background_tasks, db)
        query = urlencode({"refresh_status": state.get("status", "success")})
    except Exception as exc:
        error_message = summarize_exception(exc)
        write_clip_update_state("failed", error_message, phase="failed")
        query = urlencode({"refresh_status": "error"})
    return local_redirect(request, f"/?{query}")


def register_clip_routes() -> None:
    """Register Clips admin actions after the main FastAPI app has been constructed."""
    from app.main import app

    if getattr(app.state, "clip_routes_registered", False):
        return
    app.include_router(router)
    app.state.clip_routes_registered = True
