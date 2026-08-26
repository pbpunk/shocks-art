from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request

from app.indexing.job_queue import IndexJob, IndexJobQueue, IndexJobQueueError
from app.library_routes import url_path


router = APIRouter()
templates = Jinja2Templates(directory="templates")
templates.env.globals["url_path"] = url_path


class QueueJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_type: str
    media_id: str = ""
    limit: int | None = Field(default=None, ge=1, le=1000)
    include_remote: bool = False
    import_language: bool = True


def _queue() -> IndexJobQueue:
    return IndexJobQueue()


def _job_payload(job: IndexJob) -> dict[str, Any]:
    return {
        "jobId": job.job_id,
        "jobType": job.job_type,
        "mediaId": job.media_id or None,
        "status": job.status,
        "priority": job.priority,
        "attemptCount": job.attempt_count,
        "maxAttempts": job.max_attempts,
        "progress": job.progress,
        "result": job.result,
        "error": job.error_message or None,
        "startedAt": job.started_at or None,
        "completedAt": job.completed_at or None,
        "createdAt": job.created_at,
        "updatedAt": job.updated_at,
    }


def _enqueue_from_request(queue: IndexJobQueue, payload: QueueJobRequest) -> IndexJob:
    job_type = payload.job_type.strip().lower()
    if job_type == "visual-pending":
        return queue.enqueue(
            job_type,
            payload={"limit": payload.limit, "include_remote": payload.include_remote},
        )
    if job_type == "visual-embeddings":
        return queue.enqueue(job_type, payload={"limit": payload.limit})
    if job_type == "sync-stream-media":
        return queue.enqueue(
            job_type,
            payload={"limit": payload.limit, "import_language": payload.import_language},
        )
    if job_type == "visual-media":
        return queue.enqueue(job_type, media_id=payload.media_id)
    raise HTTPException(status_code=422, detail=f"unsupported indexing job type: {job_type}")


def _cancel_queued(queue: IndexJobQueue, job_id: str) -> IndexJob:
    timestamp = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    with queue._transaction() as connection:  # queue owns the transaction/schema contract
        row = connection.execute(
            "SELECT status FROM index_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise IndexJobQueueError(f"indexing job not found: {job_id}")
        status = str(row["status"])
        if status == "running":
            raise IndexJobQueueError(
                "running jobs cannot be force-cancelled; wait for the worker lease to finish or recover"
            )
        if status != "queued":
            raise IndexJobQueueError("only queued jobs can be cancelled")
        connection.execute(
            """
            UPDATE index_jobs
            SET status = 'cancelled', completed_at = ?, error_message = 'cancelled by user',
                lease_owner = '', lease_expires_at = '', updated_at = ?
            WHERE job_id = ? AND status = 'queued'
            """,
            (timestamp, timestamp, job_id),
        )
    return queue.get(job_id)


@router.get("/api/library/indexing/jobs")
def library_indexing_jobs(
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
):
    queue = _queue()
    return {
        "schemaVersion": 1,
        "mutatesState": False,
        "snapshot": queue.snapshot(),
        "jobs": [_job_payload(job) for job in queue.list(status=status, limit=limit)],
    }


@router.post("/api/library/indexing/jobs")
def library_indexing_enqueue(payload: QueueJobRequest):
    queue = _queue()
    try:
        job = _enqueue_from_request(queue, payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"ok": True, "job": _job_payload(job)}


@router.post("/api/library/indexing/jobs/{job_id}/retry")
def library_indexing_retry(job_id: str):
    queue = _queue()
    try:
        job = queue.retry(job_id)
    except IndexJobQueueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "job": _job_payload(job)}


@router.post("/api/library/indexing/jobs/{job_id}/cancel")
def library_indexing_cancel(job_id: str):
    queue = _queue()
    try:
        job = _cancel_queued(queue, job_id)
    except IndexJobQueueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "job": _job_payload(job)}


@router.get("/library/indexing", response_class=HTMLResponse)
def library_indexing_dashboard(request: Request):
    return templates.TemplateResponse(
        request,
        "library_indexing.html",
        {"request": request},
    )


def register_indexing_control_routes() -> None:
    from app.main import app

    if getattr(app.state, "indexing_control_routes_registered", False):
        return
    app.include_router(router)
    app.state.indexing_control_routes_registered = True
