from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.core.database import get_db, init_db
from app.main import structured_clip_candidates
from app.models import CandidateWindow
from app.services.clip_download import (
    clip_download_status,
    clip_path,
    download_filename,
    generate_clip_background,
    queue_clip_generation,
)


app = FastAPI(title="Shocks Art Public Clips", version="0.1.0")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


@app.on_event("startup")
def startup() -> None:
    init_db()


@app.get("/", response_class=HTMLResponse)
def home():
    return RedirectResponse(url="/clips", status_code=303)


@app.get("/clips", response_class=HTMLResponse)
def clips(
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
