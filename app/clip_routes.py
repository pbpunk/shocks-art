from urllib.parse import urlencode

from fastapi import APIRouter, BackgroundTasks, Depends
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.core.database import SessionLocal, get_db
from app.models import CandidateWindow, Stream
from app.services.clip_new_state import read_new_clip_ids, replace_new_clip_ids
from app.services.clip_update_state import read_clip_update_state, write_clip_update_state
from app.services.processing import discover_and_store_streams, process_queued_streams, summarize_exception


router = APIRouter()


def url_path(request: Request, path: str) -> str:
    root_path = request.scope.get("app_prefix", request.scope.get("root_path", "")).rstrip("/")
    normalized_path = path if path.startswith("/") else f"/{path}"
    return f"{root_path}{normalized_path}" if root_path else normalized_path


def local_redirect(request: Request, path: str, status_code: int = 303) -> RedirectResponse:
    return RedirectResponse(url=url_path(request, path), status_code=status_code)


def process_stream_queue_background() -> None:
    """Process queued/failed streams, update NEW state, and publish progress."""
    with SessionLocal() as db:
        before_ids = set(db.scalars(select(CandidateWindow.candidate_window_id)).all())
        try:
            result = process_queued_streams(db)
            after_ids = set(db.scalars(select(CandidateWindow.candidate_window_id)).all())
            new_ids = after_ids - before_ids
            replace_new_clip_ids(new_ids)
            message = f"Update complete: {len(new_ids)} new clip(s) surfaced."
            write_clip_update_state(
                "complete",
                message,
                new_clips=len(new_ids),
                attempted=result.get("attempted", 0),
                complete=result.get("complete", 0),
                failed=result.get("failed", 0),
            )
        except Exception as exc:
            write_clip_update_state("failed", summarize_exception(exc))


@router.get("/api/clips/new-state")
def new_clip_state_api():
    return {"candidate_window_ids": sorted(read_new_clip_ids())}


@router.get("/api/clips/update-state")
def clip_update_state_api():
    return read_clip_update_state()


@router.post("/actions/clips/refresh-streams")
def refresh_streams_action(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    write_clip_update_state("checking", "Checking for new streams…")
    try:
        result = discover_and_store_streams(db)
        pending_count = db.scalar(
            select(func.count())
            .select_from(Stream)
            .where(Stream.processing_status.in_(["queued", "failed"]))
        ) or 0

        if pending_count:
            write_clip_update_state(
                "processing",
                f"Processing {pending_count} stream(s)…",
                pending_streams=pending_count,
            )
            background_tasks.add_task(process_stream_queue_background)
            processing_message = f" {pending_count} stream(s) queued for processing."
        else:
            message = "Up to date — no streams are waiting to process."
            write_clip_update_state("complete", message, new_clips=0, pending_streams=0)
            processing_message = " Nothing new is waiting to process."

        message = (
            f"Stream check complete: {result['created']} new, {result['updated']} existing; "
            f"{result['transcripts_available']} transcripts available, "
            f"{result['transcripts_missing']} missing."
            f"{processing_message}"
        )
        query = urlencode({"refresh_status": "success", "refresh_message": message})
    except Exception as exc:
        error_message = summarize_exception(exc)
        write_clip_update_state("failed", error_message)
        query = urlencode({"refresh_status": "error", "refresh_message": error_message})

    return local_redirect(request, f"/?{query}")


def register_clip_routes() -> None:
    """Register Clips admin actions after the main FastAPI app has been constructed."""
    from app.main import app

    if getattr(app.state, "clip_routes_registered", False):
        return
    app.include_router(router)
    app.state.clip_routes_registered = True
