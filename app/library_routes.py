import os
from datetime import datetime, timezone
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
from app.indexing.service import (
    VisualExtractionConfig,
    effective_sample_interval_seconds,
    video_sample_timestamps_ms,
    visual_sampling_plan,
)
from app.library_models import Media
from app.services.library import IngestResult, ingest_local_media, scan_media_files


router = APIRouter()
templates = Jinja2Templates(directory="templates")
APP_ID = "shocks-art"
APP_NAME = "Shocks Art"
APP_ROUTE = os.getenv("APP_ROUTE", "/shocks_art")
APP_STARTED_AT = datetime.now(timezone.utc).isoformat()


def url_path(request: Request, path: str) -> str:
    root_path = request.scope.get("app_prefix", request.scope.get("root_path", "")).rstrip("/")
    normalized_path = path if path.startswith("/") else f"/{path}"
    return f"{root_path}{normalized_path}" if root_path else normalized_path


def local_redirect(request: Request, path: str, status_code: int = 303) -> RedirectResponse:
    return RedirectResponse(url=url_path(request, path), status_code=status_code)


templates.env.globals["url_path"] = url_path


def app_version() -> str:
    return os.getenv("JARVIS_COMMIT_SHA", "unknown")


def media_inventory_item(media: Media) -> dict:
    metadata = media.metadata_json if isinstance(media.metadata_json, dict) else {}
    return {
        "mediaId": media.media_id,
        "sourceType": media.source_type,
        "sourceUrl": media.source_url or None,
        "title": media.title,
        "filename": media.filename,
        "relativePath": metadata.get("relative_path"),
        "mimeType": media.mime_type,
        "kind": media.media_kind,
        "durationSeconds": media.duration_seconds,
        "dimensions": {
            "width": media.width or 0,
            "height": media.height or 0,
        },
        "sizeBytes": media.size_bytes,
        "processingStatus": media.processing_status,
        "sha256Short": media.checksum_sha256[:12] if media.checksum_sha256 else None,
        "createdAt": media.created_at.isoformat() if media.created_at else None,
        "updatedAt": media.updated_at.isoformat() if media.updated_at else None,
    }


def ingest_summary_message(result: IngestResult) -> str:
    message = (
        f"Scan complete: {result.discovered} found, {result.created} added, "
        f"{result.updated} updated, {result.skipped} unchanged, {result.errors} errors."
    )
    if result.failures:
        failure_summaries = [
            f"{failure.path}: {failure.error_type}: {failure.message[:180]}"
            for failure in result.failures[:3]
        ]
        message += " Failures: " + " | ".join(failure_summaries)
        if len(result.failures) > 3:
            message += f" | +{len(result.failures) - 3} more (see /api/library/ingest)."
    return message


@router.get("/health")
def app_health():
    """Canonical JARVIS readiness and identity endpoint."""
    return {
        "status": "ok",
        "ok": True,
        "app": APP_ID,
        "name": APP_NAME,
        "route": APP_ROUTE,
        "version": app_version(),
        "startedAt": APP_STARTED_AT,
        "mode": os.getenv("JARVIS_MODE", "production"),
        "checkedAt": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/api/ping")
def app_ping():
    """Cheap API-route verification endpoint for JARVIS startup checks."""
    return {"ok": True, "app": APP_ID, "route": APP_ROUTE, "version": app_version()}


@router.get("/api/library/media")
def library_media_inventory(
    q: str = Query(default=""),
    kind: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=1000, ge=1, le=5000),
    db: Session = Depends(get_db),
):
    """Machine-readable Media inventory without absolute workstation paths."""
    media_query = select(Media).order_by(Media.created_at.desc())
    normalized_q = q.strip().lower()
    if normalized_q:
        pattern = f"%{normalized_q}%"
        media_query = media_query.where(
            or_(
                func.lower(Media.title).like(pattern),
                func.lower(Media.filename).like(pattern),
            )
        )
    if kind:
        media_query = media_query.where(Media.media_kind == kind.strip().lower())
    if status:
        media_query = media_query.where(Media.processing_status == status.strip().lower())

    items = list(db.scalars(media_query.limit(limit)).all())
    total_count = db.scalar(select(func.count()).select_from(Media)) or 0
    video_count = db.scalar(select(func.count()).select_from(Media).where(Media.media_kind == "video")) or 0
    image_count = db.scalar(select(func.count()).select_from(Media).where(Media.media_kind == "image")) or 0

    return {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total": total_count,
            "video": video_count,
            "image": image_count,
            "returned": len(items),
        },
        "filters": {
            "q": q,
            "kind": kind,
            "status": status,
            "limit": limit,
        },
        "items": [media_inventory_item(media) for media in items],
    }


@router.get("/api/library/indexing/sampling-plan")
def library_indexing_sampling_plan(db: Session = Depends(get_db)):
    """Preview adaptive visual sampling without creating Traces or artifacts."""
    config = VisualExtractionConfig()
    media_items = list(
        db.scalars(
            select(Media)
            .where(Media.media_kind.in_(["image", "video"]))
            .order_by(Media.media_kind.asc(), Media.duration_seconds.asc(), Media.filename.asc())
        ).all()
    )
    plans = [visual_sampling_plan(media, config) for media in media_items]

    reference_durations = [
        ("30 seconds", 30.0),
        ("60 seconds", 60.0),
        ("10 minutes", 600.0),
        ("1 hour", 3600.0),
        ("3 hours", 10800.0),
        ("8 hours", 28800.0),
    ]
    references = [
        {
            "label": label,
            "durationSeconds": duration,
            "intervalSeconds": effective_sample_interval_seconds(duration, config),
            "sampleCount": len(video_sample_timestamps_ms(duration, config)),
        }
        for label, duration in reference_durations
    ]

    return {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "mutatesState": False,
        "configuration": {
            "samplingPolicy": config.sampling_policy,
            "configurationHash": config.configuration_hash,
            **config.as_payload(),
        },
        "summary": {
            "media": len(plans),
            "video": sum(1 for plan in plans if plan["kind"] == "video"),
            "image": sum(1 for plan in plans if plan["kind"] == "image"),
            "expectedVisualTraces": sum(plan["sampleCount"] for plan in plans),
            "maxVideoSamples": config.max_video_samples,
        },
        "items": plans,
        "referenceDurations": references,
    }


@router.post("/api/library/ingest")
def library_ingest_api(db: Session = Depends(get_db)):
    """Run local Library ingestion and return complete per-file diagnostics."""
    ingest_path = Path(get_settings().library_ingest_path)
    result = ingest_local_media(db, ingest_path)
    return {
        "ok": result.errors == 0,
        "result": result.as_dict(),
    }


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
        message = ingest_summary_message(result)
        status = "success" if result.errors == 0 else "warning"
    except Exception as exc:
        status = "error"
        message = f"Library scan failed: {type(exc).__name__}: {exc}"
    query = urlencode({"ingest_status": status, "ingest_message": message})
    return local_redirect(request, f"/library?{query}")


def register_library_routes() -> None:
    """Register the Library router after the main FastAPI app has been constructed."""
    from app.main import app

    if getattr(app.state, "library_routes_registered", False):
        return
    app.include_router(router)
    app.state.library_routes_registered = True
