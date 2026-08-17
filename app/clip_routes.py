from urllib.parse import urlencode

from fastapi import APIRouter, BackgroundTasks, Depends
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.core.database import SessionLocal, get_db
from app.models import Stream
from app.services.processing import discover_and_store_streams, process_queued_streams, summarize_exception


router = APIRouter()


def url_path(request: Request, path: str) -> str:
    root_path = request.scope.get("app_prefix", request.scope.get("root_path", "")).rstrip("/")
    normalized_path = path if path.startswith("/") else f"/{path}"
    return f"{root_path}{normalized_path}" if root_path else normalized_path


def local_redirect(request: Request, path: str, status_code: int = 303) -> RedirectResponse:
    return RedirectResponse(url=url_path(request, path), status_code=status_code)


def process_stream_queue_background() -> None:
    """Process queued/failed streams using a fresh DB session after the response returns."""
    with SessionLocal() as db:
        process_queued_streams(db)


@router.post("/actions/clips/refresh-streams")
def refresh_streams_action(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    try:
        result = discover_and_store_streams(db)
        pending_count = db.scalar(
            select(func.count())
            .select_from(Stream)
            .where(Stream.processing_status.in_(["queued", "failed"]))
        ) or 0

        if pending_count:
            background_tasks.add_task(process_stream_queue_background)
            processing_message = f" {pending_count} stream(s) queued for processing."
        else:
            processing_message = " Nothing new is waiting to process."

        message = (
            f"Stream check complete: {result['created']} new, {result['updated']} existing; "
            f"{result['transcripts_available']} transcripts available, "
            f"{result['transcripts_missing']} missing."
            f"{processing_message}"
        )
        query = urlencode({"refresh_status": "success", "refresh_message": message})
    except Exception as exc:
        query = urlencode({"refresh_status": "error", "refresh_message": summarize_exception(exc)})

    return local_redirect(request, f"/?{query}")


def register_clip_routes() -> None:
    """Register Clips admin actions after the main FastAPI app has been constructed."""
    from app.main import app

    if getattr(app.state, "clip_routes_registered", False):
        return
    app.include_router(router)
    app.state.clip_routes_registered = True
