from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.core.config import get_settings
from app.core.database import get_db
from app.library_models import Media
from app.services.library import ingest_local_media, scan_media_files


router = APIRouter()
templates = Jinja2Templates(directory="templates")


def url_path(request: Request, path: str) -> str:
    root_path = request.scope.get("app_prefix", request.scope.get("root_path", "")).rstrip("/")
    normalized_path = path if path.startswith("/") else f"/{path}"
    return f"{root_path}{normalized_path}" if root_path else normalized_path


def local_redirect(request: Request, path: str, status_code: int = 303) -> RedirectResponse:
    return RedirectResponse(url=url_path(request, path), status_code=status_code)


templates.env.globals["url_path"] = url_path


@router.get("/health")
def app_health():
    """Cheap readiness endpoint used by the JARVIS app contract."""
    return {"status": "ok", "app": "shocks-art"}


@router.get("/library", response_class=HTMLResponse)
def library_dashboard(
    request: Request,
    q: str = Query(default=""),
    ingest_status: str | None = Query(default=None),
    ingest_message: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    settings = get_settings()
    ingest_path = Path(settings.library_ingest_path)
    query = select(Media).order_by(Media.created_at.desc())
    normalized_q = q.strip().lower()
    if normalized_q:
        pattern = f"%{normalized_q}%"
        query = query.where(
            or_(
                func.lower(Media.title).like(pattern),
                func.lower(Media.filename).like(pattern),
            )
        )
    media_items = list(db.scalars(query).all())
    media_count = db.scalar(select(func.count()).select_from(Media)) or 0
    video_count = db.scalar(select(func.count()).select_from(Media).where(Media.media_kind == "video")) or 0
    image_count = db.scalar(select(func.count()).select_from(Media).where(Media.media_kind == "image")) or 0
    inbox_file_count = len(scan_media_files(ingest_path))
    return templates.TemplateResponse(
        request,
        "library.html",
        {
            "request": request,
            "media_items": media_items,
            "media_count": media_count,
            "video_count": video_count,
            "image_count": image_count,
            "inbox_file_count": inbox_file_count,
            "ingest_path": str(ingest_path),
            "query": q,
            "ingest_status": ingest_status,
            "ingest_message": ingest_message,
        },
    )


@router.post("/actions/library/ingest")
def library_ingest_action(request: Request, db: Session = Depends(get_db)):
    ingest_path = Path(get_settings().library_ingest_path)
    try:
        result = ingest_local_media(db, ingest_path)
        message = (
            f"Scan complete: {result.discovered} found, {result.created} added, "
            f"{result.updated} updated, {result.skipped} unchanged, {result.errors} errors."
        )
        status = "success" if result.errors == 0 else "warning"
    except Exception as exc:
        status = "error"
        message = f"Library scan failed: {exc}"
    query = urlencode({"ingest_status": status, "ingest_message": message})
    return local_redirect(request, f"/library?{query}")


def register_library_routes() -> None:
    """Register the Library router after the main FastAPI app has been constructed."""
    from app.main import app

    if getattr(app.state, "library_routes_registered", False):
        return
    app.include_router(router)
    app.state.library_routes_registered = True
