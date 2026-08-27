from threading import Lock
from urllib.parse import urlencode

from fastapi import APIRouter, BackgroundTasks, Depends
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.core.database import SessionLocal, get_db
from app.models import Stream
from app.services.clip_new_state import read_new_clip_ids, replace_new_clip_ids
from app.services.clip_update_state import read_clip_update_state, write_clip_update_state
from app.services.clips_native_ask import (
    pending_native_ask_streams,
    production_candidate_ids,
    production_clip_candidates,
    run_clips_native_ask,
)
from app.services.processing import discover_and_store_streams, summarize_exception


router = APIRouter()
UPDATE_LOCK = Lock()
ACTIVE_UPDATE_STATUSES = {"checking", "processing"}
LEGACY_DIRECT_GEMINI_DETAIL = (
    "Direct Gemini video analysis is disabled for Shocks Art Clips. "
    "Use Clips Update, which analyzes the YouTube page through its native Ask interaction."
)


def url_path(request: Request, path: str) -> str:
    root_path = request.scope.get("app_prefix", request.scope.get("root_path", "")).rstrip("/")
    normalized_path = path if path.startswith("/") else f"/{path}"
    return f"{root_path}{normalized_path}" if root_path else normalized_path


def local_redirect(request: Request, path: str, status_code: int = 303) -> RedirectResponse:
    return RedirectResponse(url=url_path(request, path), status_code=status_code)


def pending_streams(db: Session) -> list[Stream]:
    """Compatibility name for the production YouTube Ask pending-stream query."""

    return pending_native_ask_streams(db)


def live_update_state() -> dict:
    state = read_clip_update_state()
    if state.get("status") in ACTIVE_UPDATE_STATUSES and not UPDATE_LOCK.locked():
        return write_clip_update_state(
            "failed",
            "The previous update was interrupted by a server restart.",
            phase="interrupted",
        )
    return state


def process_stream_queue_background() -> None:
    """Process waiting streams sequentially through the native YouTube Ask UI."""

    try:
        with SessionLocal() as db:
            streams = [(stream.stream_id, stream.title) for stream in pending_streams(db)]
            before_ids = production_candidate_ids(db)

        total = len(streams)
        completed = 0
        failed = 0

        try:
            for index, (stream_id, stream_title) in enumerate(streams, start=1):
                write_clip_update_state(
                    "processing",
                    f"Asking YouTube {index}/{total}: {stream_title}",
                    phase="youtube_ask",
                    total_streams=total,
                    current_index=index,
                    completed_streams=completed,
                    failed_streams=failed,
                    current_stream_id=stream_id,
                    current_stream_title=stream_title,
                )

                result = run_clips_native_ask(stream_id)
                if result.get("status") == "complete":
                    completed += 1
                else:
                    failed += 1

                write_clip_update_state(
                    "processing",
                    f"Finished {index}/{total}: {stream_title}",
                    phase="between_streams",
                    total_streams=total,
                    current_index=index,
                    completed_streams=completed,
                    failed_streams=failed,
                    current_stream_id=stream_id,
                    current_stream_title=stream_title,
                )

            with SessionLocal() as db:
                after_ids = production_candidate_ids(db)
            new_ids = after_ids - before_ids
            replace_new_clip_ids(new_ids)

            message = f"Update complete: {len(new_ids)} new YouTube Ask clip(s) surfaced."
            if failed:
                message += f" {failed} stream(s) failed and remain eligible for retry."
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
    finally:
        if UPDATE_LOCK.locked():
            UPDATE_LOCK.release()


def begin_clip_update(background_tasks: BackgroundTasks, db: Session) -> dict:
    if not UPDATE_LOCK.acquire(blocking=False):
        return live_update_state()

    try:
        write_clip_update_state("checking", "Checking for new streams…", phase="discovering")
        result = discover_and_store_streams(db)
        streams = pending_streams(db)
        pending_count = len(streams)

        if pending_count:
            state = write_clip_update_state(
                "processing",
                f"Preparing {pending_count} stream(s) for YouTube Ask…",
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

        replace_new_clip_ids(set())
        state = write_clip_update_state(
            "complete",
            "Up to date — every discovered stream has a completed YouTube Ask run.",
            phase="complete",
            new_clips=0,
            total_streams=0,
            current_index=0,
            completed_streams=0,
            failed_streams=0,
            pending_streams=0,
            discovered_streams=result.get("created", 0),
        )
        UPDATE_LOCK.release()
        return state
    except Exception:
        if UPDATE_LOCK.locked():
            UPDATE_LOCK.release()
        raise


@router.get("/api/clips/new-state")
def new_clip_state_api():
    return {"candidate_window_ids": sorted(read_new_clip_ids())}


@router.get("/api/clips/update-state")
def clip_update_state_api():
    return live_update_state()


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


def _is_legacy_direct_gemini_path(path: str) -> bool:
    normalized = path.rstrip("/")
    if normalized.endswith("/api/process") or normalized.endswith("/actions/process-one"):
        return True
    return "/api/streams/" in normalized and normalized.endswith("/analyze")


def register_clip_routes() -> None:
    """Register Clips routes and enforce native-Ask-only production lineage."""

    import app.main as main_module

    app = main_module.app
    if getattr(app.state, "clip_routes_registered", False):
        return

    # dashboard_clips_home resolves this module global at request time, so replacing it
    # here makes legacy direct-Gemini candidates immediately disappear from production
    # Clips without deleting their historical lineage from the database.
    main_module.structured_clip_candidates = production_clip_candidates

    @app.middleware("http")
    async def block_legacy_direct_gemini_routes(request: Request, call_next):
        if request.method.upper() == "POST" and _is_legacy_direct_gemini_path(request.scope.get("path", "")):
            return JSONResponse(status_code=410, content={"detail": LEGACY_DIRECT_GEMINI_DETAIL})
        return await call_next(request)

    app.include_router(router)
    app.state.clip_routes_registered = True
