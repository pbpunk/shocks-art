import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Query, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db, init_db
from app.models import AnalysisRun, CandidateWindow, Stream
from app.services.clip_download import (
    clip_download_status,
    clip_path,
    download_filename,
    generate_clip_background,
    queue_clip_generation,
)
from app.services.export import export_csv, export_json
from app.services.gemini import GeminiAnalyzer, debug_log_path
from app.services.native_automation import (
    open_native_profile_setup,
    read_native_job_status,
    read_profile_setup_status,
    run_native_ask_background,
    write_native_job_status,
)
from app.services.native_youtube import build_native_youtube_prompt, save_native_youtube_response
from app.services.processing import (
    analyze_one_stream,
    discover_and_store_streams,
    process_queued_streams,
    run_analysis_background,
    start_next_analysis_run,
    summarize_exception,
)
from app.services.ranking import rank_candidates
from app.services.repository import candidate_query, create_analysis_run, save_candidate, upsert_stream
from app.services.tags import normalize_tags
from app.services.validation import validate_candidate_response
from app.services.video_probe import read_probe, run_video_access_probe
from app.services.youtube_analytics import (
    analytics_overview,
    build_oauth_authorization_url,
    exchange_oauth_callback,
    import_youtube_studio_csv,
    livestream_detail,
    refresh_available_channels,
    run_youtube_analytics_sync_background,
    select_analytics_channel,
    sync_youtube_analytics,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Shocks Art Livestream Content System", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


APP_PREFIX = "/shocks_art"
TAILSCALE_HOST = "desktop.tail27cee7.ts.net"


def url_path(request: Request, path: str) -> str:
    root_path = request.scope.get("app_prefix", request.scope.get("root_path", "")).rstrip("/")
    normalized_path = path if path.startswith("/") else f"/{path}"
    return f"{root_path}{normalized_path}" if root_path else normalized_path


def local_redirect(request: Request, path: str, status_code: int = 303) -> RedirectResponse:
    return RedirectResponse(url=url_path(request, path), status_code=status_code)


templates.env.globals["url_path"] = url_path


@app.middleware("http")
async def app_prefix_middleware(request: Request, call_next):
    path = request.scope.get("path", "")
    host = request.headers.get("host", "").split(":", 1)[0]
    if host == TAILSCALE_HOST:
        request.scope["app_prefix"] = APP_PREFIX
    if path == APP_PREFIX:
        return RedirectResponse(url=f"{APP_PREFIX}/", status_code=307)
    if path.startswith(f"{APP_PREFIX}/"):
        request.scope["app_prefix"] = APP_PREFIX
        request.scope["path"] = path[len(APP_PREFIX) :] or "/"
    return await call_next(request)


DEBUG_STATE_PATH = Path("data/debug_console_state.json")


def read_debug_cleared_at() -> datetime | None:
    if not DEBUG_STATE_PATH.exists():
        return None
    try:
        value = json.loads(DEBUG_STATE_PATH.read_text(encoding="utf-8")).get("cleared_at")
        return datetime.fromisoformat(value) if value else None
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def write_debug_cleared_at() -> None:
    DEBUG_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    DEBUG_STATE_PATH.write_text(
        json.dumps({"cleared_at": datetime.now(timezone.utc).isoformat()}),
        encoding="utf-8",
    )


@app.get("/pipeline", response_class=HTMLResponse)
def pipeline_dashboard(
    request: Request,
    archive_status: str | None = Query(default=None),
    archive_message: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    statuses = dict(db.execute(select(Stream.processing_status, func.count()).group_by(Stream.processing_status)).all())
    all_candidates = list(db.scalars(select(CandidateWindow)).all())
    candidates = rank_candidates(all_candidates, limit=5)
    recent_streams = list(db.scalars(select(Stream).order_by(Stream.published_at.desc()).limit(8)).all())
    recent_runs = list(db.scalars(select(AnalysisRun).order_by(AnalysisRun.created_at.desc()).limit(8)).all())
    active_runs = [run for run in recent_runs if run.status == "processing"]
    candidate_count = len(all_candidates)
    approved_count = sum(1 for candidate in all_candidates if candidate.review_status == "approved")
    needs_verification_count = sum(1 for candidate in all_candidates if candidate.review_status == "needs_verification")
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "request": request,
            "statuses": statuses,
            "stream_count": sum(statuses.values()),
            "candidate_count": candidate_count,
            "approved_count": approved_count,
            "needs_verification_count": needs_verification_count,
            "top_candidates": candidates,
            "recent_streams": recent_streams,
            "recent_runs": recent_runs,
            "active_runs": active_runs,
            "archive_status": archive_status,
            "archive_message": archive_message,
            "settings": get_settings(),
        },
    )


def structured_clip_candidates(db: Session, limit: int, include_check: bool) -> tuple[list[CandidateWindow], int]:
    query = select(CandidateWindow).where(CandidateWindow.review_status != "archived")
    if not include_check:
        query = query.where(CandidateWindow.review_status != "needs_verification")
    candidates = list(db.scalars(query).all())
    candidates.sort(key=lambda c: (c.stream.published_at, c.weighted_score), reverse=True)
    return candidates[:limit], len(candidates)


@app.get("/", response_class=HTMLResponse)
def dashboard_clips_home(
    request: Request,
    limit: int = Query(default=200, ge=1, le=200),
    include_check: bool = Query(default=True),
    db: Session = Depends(get_db),
):
    candidates, total_count = structured_clip_candidates(db, limit, include_check)
    return templates.TemplateResponse(
        request,
        "dashboard_clips.html",
        {
            "request": request,
            "candidates": candidates,
            "total_count": total_count,
            "limit": limit,
            "include_check": include_check,
        },
    )


@app.get("/analytics", response_class=HTMLResponse)
def analytics_dashboard(
    request: Request,
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    analytics_status: str | None = Query(default=None),
    analytics_message: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    overview = analytics_overview(db, start_date=start_date, end_date=end_date)
    return templates.TemplateResponse(
        request,
        "analytics.html",
        {
            "request": request,
            "overview": overview,
            "analytics_status": analytics_status,
            "analytics_message": analytics_message,
            "settings": get_settings(),
        },
    )


@app.get("/analytics/livestreams/{video_id}", response_class=HTMLResponse)
def analytics_livestream_dashboard(video_id: str, request: Request, db: Session = Depends(get_db)):
    detail = livestream_detail(db, video_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Livestream analytics not found")
    return templates.TemplateResponse(
        request,
        "analytics_livestream.html",
        {"request": request, "detail": detail},
    )


@app.get("/analytics/connect/youtube")
def analytics_connect_youtube(request: Request):
    try:
        return RedirectResponse(url=build_oauth_authorization_url(), status_code=303)
    except Exception as exc:
        query = urlencode({"analytics_status": "error", "analytics_message": str(exc)})
        return local_redirect(request, f"/analytics?{query}")


@app.get("/analytics/oauth2callback")
def analytics_oauth_callback(request: Request, state: str | None = Query(default=None), db: Session = Depends(get_db)):
    try:
        credential = exchange_oauth_callback(db, str(request.url), state)
        query = urlencode(
            {
                "analytics_status": "success",
                "analytics_message": f"Connected YouTube Analytics for {credential.channel_title or credential.channel_id}.",
            }
        )
    except Exception as exc:
        query = urlencode({"analytics_status": "error", "analytics_message": str(exc)})
    return local_redirect(request, f"/analytics?{query}")


@app.post("/actions/analytics/sync-youtube")
def analytics_sync_youtube_action(request: Request, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    overview = analytics_overview(db)
    if not overview["connection"]["connected"]:
        query = urlencode({"analytics_status": "error", "analytics_message": "Connect YouTube Analytics before syncing."})
        return local_redirect(request, f"/analytics?{query}")
    latest_sync = overview["connection"]["latest_sync"]
    if latest_sync and latest_sync["status"] == "processing":
        query = urlencode({"analytics_status": "success", "analytics_message": "YouTube Analytics sync is already running."})
        return local_redirect(request, f"/analytics?{query}")
    background_tasks.add_task(run_youtube_analytics_sync_background, "manual")
    query = urlencode({"analytics_status": "success", "analytics_message": "YouTube Analytics sync started."})
    return local_redirect(request, f"/analytics?{query}")


@app.post("/actions/analytics/refresh-channels")
def analytics_refresh_channels_action(request: Request, db: Session = Depends(get_db)):
    try:
        channels = refresh_available_channels(db)
        query = urlencode({"analytics_status": "success", "analytics_message": f"Found {len(channels)} available YouTube channel(s)."})
    except Exception as exc:
        query = urlencode({"analytics_status": "error", "analytics_message": str(exc)})
    return local_redirect(request, f"/analytics?{query}")


@app.post("/actions/analytics/select-channel")
def analytics_select_channel_action(request: Request, channel_id: str = Form(...), db: Session = Depends(get_db)):
    try:
        credential = select_analytics_channel(db, channel_id)
        query = urlencode({"analytics_status": "success", "analytics_message": f"Selected analytics channel: {credential.channel_title or credential.channel_id}."})
    except Exception as exc:
        query = urlencode({"analytics_status": "error", "analytics_message": str(exc)})
    return local_redirect(request, f"/analytics?{query}")


@app.post("/actions/analytics/import-csv")
async def analytics_import_csv_action(
    request: Request,
    import_type: str = Form(default="daily"),
    csv_file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    try:
        content = (await csv_file.read()).decode("utf-8-sig")
        result = import_youtube_studio_csv(db, content, filename=csv_file.filename or "", import_type=import_type)
        if result.status == "failed":
            query = urlencode({"analytics_status": "error", "analytics_message": result.error_message})
        else:
            message = (
                f"Imported {result.rows_fetched} CSV row(s), updated {result.videos_updated} video(s), "
                f"{result.livestreams_updated} livestream(s), and {result.timeseries_points_updated} timeline point(s)."
            )
            if result.error_message:
                message = f"{message} {result.error_message}"
            query = urlencode({"analytics_status": "success", "analytics_message": message})
    except UnicodeDecodeError:
        query = urlencode({"analytics_status": "error", "analytics_message": "Upload a UTF-8 CSV exported from YouTube Studio."})
    except Exception as exc:
        query = urlencode({"analytics_status": "error", "analytics_message": str(exc)})
    return local_redirect(request, f"/analytics?{query}")


@app.post("/api/analytics/sync-youtube")
def analytics_sync_youtube_api(db: Session = Depends(get_db)):
    result = sync_youtube_analytics(db, mode="manual")
    if result.status == "failed":
        raise HTTPException(status_code=503, detail=result.error_message)
    return result.__dict__


@app.get("/api/analytics/overview")
def analytics_overview_api(
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    return analytics_overview(db, start_date=start_date, end_date=end_date)


@app.get("/api/analytics/livestreams/{video_id}")
def analytics_livestream_api(video_id: str, db: Session = Depends(get_db)):
    detail = livestream_detail(db, video_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Livestream analytics not found")
    return detail


@app.get("/debug", response_class=HTMLResponse)
def debug_console(request: Request, run_id: str | None = Query(default=None), db: Session = Depends(get_db)):
    runs = list(db.scalars(select(AnalysisRun).order_by(AnalysisRun.created_at.desc()).limit(30)).all())
    selected_run = db.get(AnalysisRun, run_id) if run_id else (runs[0] if runs else None)
    prompt_text = ""
    raw_response = ""
    debug_log = ""
    probe_log = ""
    if selected_run:
        analyzer = GeminiAnalyzer("", selected_run.model, selected_run.schema_version)
        prompt_text = analyzer.build_analysis_prompt(selected_run.stream)
        log_path = debug_log_path(selected_run.analysis_run_id)
        if log_path.exists():
            debug_log = log_path.read_text(encoding="utf-8")
        if selected_run.raw_response_location and Path(selected_run.raw_response_location).exists():
            raw_response = Path(selected_run.raw_response_location).read_text(encoding="utf-8")
        probe_log = read_probe(selected_run.stream_id)
    return templates.TemplateResponse(
        request,
        "debug.html",
        {
            "request": request,
            "runs": runs,
            "selected_run": selected_run,
            "prompt_text": prompt_text,
            "raw_response": raw_response,
            "debug_log": debug_log,
            "probe_log": probe_log,
            "settings": get_settings(),
        },
    )


@app.get("/candidates", response_class=HTMLResponse)
def candidates(
    request: Request,
    pillar: str | None = Query(default=None),
    tag: str | None = Query(default=None),
    method: str | None = Query(default=None),
    review_status: str | None = Query(default=None),
    sort: str = Query(default="weighted_score"),
    db: Session = Depends(get_db),
):
    candidates = candidate_query(db, pillar=pillar, tag=tag, method=method, review_status=review_status)
    if sort == "published_at":
        candidates.sort(key=lambda c: c.stream.published_at, reverse=True)
    elif sort in {"confidence", "weighted_score"}:
        candidates.sort(key=lambda c: getattr(c, sort), reverse=True)
    elif sort in {
        "pillar_relevance",
        "hook_strength",
        "standalone_clarity",
        "visual_quality",
        "audio_clarity",
        "emotional_impact",
        "educational_value",
        "entertainment_value",
        "editing_potential",
        "brand_fit",
    }:
        candidates.sort(key=lambda c: c.scores.get(sort, 0), reverse=True)
    tags = sorted({tag for candidate in db.scalars(select(CandidateWindow)).all() for tag in candidate.tags})
    methods = [
        row[0]
        for row in db.execute(select(AnalysisRun.model).distinct().order_by(AnalysisRun.model)).all()
        if row[0]
    ]
    return templates.TemplateResponse(
        request,
        "candidates.html",
        {
            "request": request,
            "candidates": candidates,
            "pillar": pillar,
            "tag": tag,
            "method": method,
            "review_status": review_status,
            "sort": sort,
            "tags": tags,
            "methods": methods,
        },
    )


@app.get("/clips", response_class=HTMLResponse)
def public_clips(
    request: Request,
    limit: int = Query(default=200, ge=1, le=200),
    include_check: bool = Query(default=True),
    db: Session = Depends(get_db),
):
    candidates, total_count = structured_clip_candidates(db, limit, include_check)
    return templates.TemplateResponse(
        request,
        "public_clips.html",
        {
            "request": request,
            "candidates": candidates,
            "total_count": total_count,
            "limit": limit,
            "include_check": include_check,
        },
    )


@app.get("/dashboard/clips", response_class=HTMLResponse)
def dashboard_clips(
    request: Request,
    limit: int = Query(default=200, ge=1, le=200),
    include_check: bool = Query(default=True),
    db: Session = Depends(get_db),
):
    candidates, total_count = structured_clip_candidates(db, limit, include_check)
    return templates.TemplateResponse(
        request,
        "dashboard_clips.html",
        {
            "request": request,
            "candidates": candidates,
            "total_count": total_count,
            "limit": limit,
            "include_check": include_check,
        },
    )


@app.get("/native-ask", response_class=HTMLResponse)
def native_ask(request: Request):
    return local_redirect(request, "/pipeline")


@app.get("/api/streams/{stream_id}/native-prompt")
def native_prompt(stream_id: str, db: Session = Depends(get_db)):
    stream = db.get(Stream, stream_id)
    if not stream:
        raise HTTPException(status_code=404, detail="Stream not found")
    return {
        "stream_id": stream.stream_id,
        "source_video_id": stream.source_video_id,
        "title": stream.title,
        "url": stream.url,
        "prompt": build_native_youtube_prompt(stream),
    }


@app.post("/api/clips/{candidate_id}/favorite")
def toggle_clip_favorite(candidate_id: str, db: Session = Depends(get_db)):
    candidate = db.get(CandidateWindow, candidate_id)
    if not candidate or candidate.review_status == "archived":
        raise HTTPException(status_code=404, detail="Clip not found")
    candidate.is_favorite = not candidate.is_favorite
    db.commit()
    return {"candidate_window_id": candidate.candidate_window_id, "is_favorite": candidate.is_favorite}


@app.get("/api/clips/{candidate_id}/download-status")
def clip_download_status_api(candidate_id: str, db: Session = Depends(get_db)):
    candidate = db.get(CandidateWindow, candidate_id)
    if not candidate or candidate.review_status == "archived":
        raise HTTPException(status_code=404, detail="Clip not found")
    status = clip_download_status(db, candidate)
    db.commit()
    return status


@app.post("/api/clips/{candidate_id}/generate-download")
def generate_clip_download_api(
    candidate_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    candidate = db.get(CandidateWindow, candidate_id)
    if not candidate or candidate.review_status == "archived":
        raise HTTPException(status_code=404, detail="Clip not found")
    status = queue_clip_generation(db, candidate)
    if status["status"] == "processing" and status.get("queued"):
        background_tasks.add_task(generate_clip_background, candidate_id)
    return status


@app.get("/api/clips/{candidate_id}/download")
def download_clip_api(candidate_id: str, db: Session = Depends(get_db)):
    candidate = db.get(CandidateWindow, candidate_id)
    if not candidate or candidate.review_status == "archived":
        raise HTTPException(status_code=404, detail="Clip not found")
    path = clip_path(candidate_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Clip download is not ready")
    return FileResponse(path, media_type="video/mp4", filename=download_filename(candidate))


@app.post("/api/native/import")
def native_import_api(
    stream_id: str = Form(...),
    response_text: str = Form(...),
    source: str = Form("native-youtube-gemini-sidebar-automated"),
    db: Session = Depends(get_db),
):
    stream = db.get(Stream, stream_id)
    if not stream:
        raise HTTPException(status_code=404, detail="Stream not found")
    try:
        result = save_native_youtube_response(db, stream, response_text, source=source)
    except Exception as exc:
        db.commit()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.commit()
    return {
        "analysis_run_id": result.run.analysis_run_id,
        "candidate_window_ids": [candidate.candidate_window_id for candidate in result.candidates],
        "skipped_duplicates": result.skipped_duplicates,
    }


@app.post("/actions/native/import")
def native_import_action(
    request: Request,
    stream_id: str = Form(...),
    response_text: str = Form(...),
    source: str = Form("native-youtube-gemini-sidebar-manual"),
    db: Session = Depends(get_db),
):
    stream = db.get(Stream, stream_id)
    if not stream:
        raise HTTPException(status_code=404, detail="Stream not found")
    try:
        result = save_native_youtube_response(db, stream, response_text, source=source)
    except Exception as exc:
        db.commit()
        return local_redirect(request, "/pipeline")
    db.commit()
    return local_redirect(request, f"/candidates?native_run_id={result.run.analysis_run_id}")


@app.post("/actions/native/run")
def native_run_action(
    request: Request,
    background_tasks: BackgroundTasks,
    stream_id: str = Form(...),
    db: Session = Depends(get_db),
):
    stream = db.get(Stream, stream_id)
    if not stream:
        raise HTTPException(status_code=404, detail="Stream not found")
    existing = read_native_job_status(stream_id)
    if existing.get("status") in {"starting", "running"}:
        return local_redirect(request, "/pipeline")
    write_native_job_status(
        stream_id,
        stream_id=stream_id,
        status="queued",
        message="Native YouTube Ask automation was queued.",
        video_url=stream.url,
    )
    background_tasks.add_task(run_native_ask_background, stream_id)
    return local_redirect(request, "/pipeline")


@app.post("/actions/native/setup-profile")
def native_setup_profile_action(request: Request, stream_id: str = Form("")):
    open_native_profile_setup()
    return local_redirect(request, "/pipeline")


@app.post("/api/discover")
def discover(db: Session = Depends(get_db)):
    try:
        return discover_and_store_streams(db)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=summarize_exception(exc)) from exc


@app.post("/api/process")
def process(limit: int | None = Query(default=None, ge=1), db: Session = Depends(get_db)):
    return process_queued_streams(db, limit=limit)


@app.post("/actions/discover")
def discover_action(request: Request, db: Session = Depends(get_db)):
    try:
        result = discover_and_store_streams(db)
        message = (
            f"Archive refreshed: {result['created']} new, {result['updated']} updated, "
            f"{result['transcripts_available']} transcripts available, "
            f"{result['transcripts_missing']} missing."
        )
        query = urlencode({"archive_status": "success", "archive_message": message})
        return local_redirect(request, f"/pipeline?{query}")
    except Exception as exc:
        query = urlencode({"archive_status": "error", "archive_message": summarize_exception(exc)})
        return local_redirect(request, f"/pipeline?{query}")


@app.post("/actions/process-one")
def process_one_action(request: Request, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    run = start_next_analysis_run(db)
    if run:
        background_tasks.add_task(run_analysis_background, run.analysis_run_id)
    return local_redirect(request, "/")


@app.post("/actions/debug/clear")
def clear_debug_console(request: Request):
    write_debug_cleared_at()
    return local_redirect(request, "/")


@app.post("/actions/debug/probe")
def run_probe(request: Request, run_id: str = Form(...), db: Session = Depends(get_db)):
    run = db.get(AnalysisRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Analysis run not found")
    run_video_access_probe(run.stream)
    return local_redirect(request, f"/debug?run_id={run_id}")


@app.post("/api/streams/{stream_id}/analyze")
def analyze(stream_id: str, db: Session = Depends(get_db)):
    try:
        candidates = analyze_one_stream(db, stream_id)
        return {"candidate_window_ids": [candidate.candidate_window_id for candidate in candidates]}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/candidates/{candidate_id}/review")
def update_review(
    request: Request,
    candidate_id: str,
    review_status: str = Form(...),
    start_seconds: int = Form(...),
    end_seconds: int = Form(...),
    primary_pillar: str = Form(...),
    tags: str = Form(""),
    reviewer_notes: str = Form(""),
    db: Session = Depends(get_db),
):
    candidate = db.get(CandidateWindow, candidate_id)
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
    candidate.review_status = review_status
    candidate.start_seconds = start_seconds
    candidate.end_seconds = end_seconds
    candidate.duration_seconds = end_seconds - start_seconds
    candidate.primary_pillar = primary_pillar
    candidate.tags = normalize_tags([tag for tag in tags.split(",") if tag.strip()])
    candidate.reviewer_notes = reviewer_notes
    db.commit()
    return local_redirect(request, "/candidates")


@app.post("/api/fixtures/load")
def load_fixture(db: Session = Depends(get_db)):
    stream, _ = upsert_stream(
        db,
        {
            "platform": "youtube",
            "channel_id": "fixture_channel",
            "source_video_id": "fixture_stream_001",
            "title": "Fixture Livestream: Wood Burning Recovery Story",
            "description": "Local fixture for MVP review and export testing.",
            "url": "https://www.youtube.com/watch?v=fixture_stream_001",
            "published_at": "2026-07-31T12:00:00Z",
            "duration": 7200,
            "thumbnail": "",
            "processing_status": "queued",
            "schema_version": "1.0",
        },
    )
    db.commit()
    return {"stream_id": stream.stream_id}


@app.post("/api/fixtures/load-candidate")
def load_fixture_candidate(db: Session = Depends(get_db)):
    stream_id = load_fixture(db)["stream_id"]
    stream = db.get(Stream, stream_id)
    existing = db.scalar(select(CandidateWindow).where(CandidateWindow.stream_id == stream_id))
    if existing:
        return {"candidate_window_id": existing.candidate_window_id}
    payload = json.loads(Path("fixtures/gemini_candidate_valid.json").read_text(encoding="utf-8"))
    payload["stream_id"] = stream_id
    run = create_analysis_run(db, stream, "fixture-gemini", "1.0", "1.0")
    candidate = save_candidate(db, run, validate_candidate_response(payload))
    db.commit()
    return {"candidate_window_id": candidate.candidate_window_id}


@app.get("/api/exports/candidates.json")
def candidates_json(db: Session = Depends(get_db)):
    content = export_json(list(db.scalars(select(CandidateWindow)).all()))
    return Response(content=content, media_type="application/json")


@app.get("/api/exports/candidates.csv")
def candidates_csv(db: Session = Depends(get_db)):
    content = export_csv(list(db.scalars(select(CandidateWindow)).all()))
    return Response(
        content=content,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=shocks_art_candidates.csv"},
    )
