from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

LIVE_ROOT = Path(os.getenv("SHOCKS_HOST_LIVE_ROOT", Path(__file__).resolve().parents[2])).resolve()
os.chdir(LIVE_ROOT)
if str(LIVE_ROOT) not in sys.path:
    sys.path.insert(0, str(LIVE_ROOT))

from app.core.database import SessionLocal
from app.indexing.qwen_backend import QwenSubprocessEmbeddingBackend
from app.library_models import Embedding, Media, Trace


BASE_URL = os.getenv("SHOCKS_RETRIEVAL_COVERAGE_BASE_URL", "http://127.0.0.1:8000/shocks_art").rstrip("/")
SCRATCH = Path(os.getenv("LIBRARY_SCRATCH_PATH", LIVE_ROOT / "data" / "library_scratch"))
JOB_TIMEOUT_SECONDS = 1500
TERMINAL_JOB_STATES = frozenset({"completed", "failed", "cancelled"})
TARGETS = (
    {
        "queryId": "fractal-burning-setup",
        "mediaId": "media_4a2b9b61b1cd44e7bd820ed68dbf207d",
    },
    {
        "queryId": "finished-staffs",
        "mediaId": "media_0a571dc5e48942fc9b9d98e27609eeb0",
    },
    {
        "queryId": "gluing-sign",
        "mediaId": "media_53c498d982c14ec680bacf2be2f4dfa0",
    },
)


def emit(payload: dict[str, Any], code: int = 0) -> int:
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    return code


def request_json(
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout: int = 30,
) -> tuple[int, dict[str, Any]]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        BASE_URL + path,
        data=body,
        headers={"Content-Type": "application/json"} if body is not None else {},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            value = json.loads(response.read().decode("utf-8"))
            return int(response.status), value if isinstance(value, dict) else {}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return 0, {}


def scratch_bytes() -> int:
    if not SCRATCH.exists():
        return 0
    return sum(path.stat().st_size for path in SCRATCH.rglob("*") if path.is_file())


def queue_snapshot() -> tuple[bool, dict[str, Any]]:
    status, payload = request_json("/api/library/indexing/jobs?limit=100")
    return status == 200, payload


def enqueue_job(job_type: str, *, media_id: str) -> str:
    status, payload = request_json(
        "/api/library/indexing/jobs",
        method="POST",
        payload={"job_type": job_type, "media_id": media_id},
    )
    if status != 200:
        return ""
    job = payload.get("job", {})
    return str(job.get("jobId") or "") if isinstance(job, dict) else ""


def wait_for_job(job_id: str, *, timeout: int = JOB_TIMEOUT_SECONDS) -> tuple[str, dict[str, Any]]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ok, payload = queue_snapshot()
        if ok:
            for job in payload.get("jobs", []):
                if str(job.get("jobId") or "") != job_id:
                    continue
                status = str(job.get("status") or "")
                if status in TERMINAL_JOB_STATES:
                    return status, job if isinstance(job, dict) else {}
        time.sleep(2)
    return "timeout", {}


def counts_for_media(db, *, media_id: str, model_id: str, dimension: int) -> dict[str, int]:
    language = int(
        db.scalar(
            select(func.count(Trace.trace_id)).where(
                Trace.media_id == media_id,
                Trace.trace_type == "language",
            )
        )
        or 0
    )
    visual = int(
        db.scalar(
            select(func.count(Trace.trace_id)).where(
                Trace.media_id == media_id,
                Trace.trace_type == "visual",
            )
        )
        or 0
    )
    exact_embeddings = int(
        db.scalar(
            select(func.count(Embedding.embedding_id))
            .join(Trace, Embedding.trace_id == Trace.trace_id)
            .where(
                Trace.media_id == media_id,
                Trace.trace_type == "visual",
                Embedding.model_id == model_id,
                Embedding.embedding_dimension == dimension,
                Embedding.normalized.is_(True),
            )
        )
        or 0
    )
    return {
        "languageTraces": language,
        "visualTraces": visual,
        "exactGenerationVisualEmbeddings": exact_embeddings,
    }


def safe_job_payload(status: str, job: dict[str, Any], job_id: str) -> dict[str, Any]:
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    return {
        "jobId": job_id or None,
        "status": status,
        "error": job.get("error") or None,
        "result": result,
    }


def process_target(*, target: dict[str, str], model_id: str, dimension: int, scratch_initial: int) -> tuple[bool, dict[str, Any]]:
    media_id = target["mediaId"]
    with SessionLocal() as db:
        media = db.get(Media, media_id)
        if media is None:
            return False, {**target, "error": "fixed Media is not present"}
        if media.source_type != "youtube":
            return False, {**target, "error": "fixed Media is not canonical YouTube Media"}
        before = counts_for_media(db, media_id=media_id, model_id=model_id, dimension=dimension)
    if before["languageTraces"] <= 0:
        return False, {**target, "before": before, "error": "fixed Media has no Language Traces"}

    extraction_status = "already-present"
    extraction_job: dict[str, Any] = {}
    extraction_job_id = ""
    if before["visualTraces"] <= 0:
        extraction_job_id = enqueue_job("visual-media", media_id=media_id)
        if not extraction_job_id:
            return False, {**target, "before": before, "error": "could not enqueue targeted visual extraction"}
        extraction_status, extraction_job = wait_for_job(extraction_job_id)
        if extraction_status != "completed":
            return False, {
                **target,
                "before": before,
                "extraction": safe_job_payload(extraction_status, extraction_job, extraction_job_id),
                "error": "targeted visual extraction did not complete",
            }

    with SessionLocal() as db:
        after_extraction = counts_for_media(db, media_id=media_id, model_id=model_id, dimension=dimension)
    if after_extraction["visualTraces"] <= 0:
        return False, {
            **target,
            "before": before,
            "afterExtraction": after_extraction,
            "error": "targeted extraction produced no visual Traces",
        }

    embedding_status = "already-present"
    embedding_job: dict[str, Any] = {}
    embedding_job_id = ""
    if after_extraction["exactGenerationVisualEmbeddings"] < after_extraction["visualTraces"]:
        embedding_job_id = enqueue_job("visual-embeddings", media_id=media_id)
        if not embedding_job_id:
            return False, {
                **target,
                "before": before,
                "afterExtraction": after_extraction,
                "error": "could not enqueue targeted visual embeddings",
            }
        embedding_status, embedding_job = wait_for_job(embedding_job_id)
        if embedding_status != "completed":
            return False, {
                **target,
                "before": before,
                "afterExtraction": after_extraction,
                "extraction": safe_job_payload(extraction_status, extraction_job, extraction_job_id),
                "embedding": safe_job_payload(embedding_status, embedding_job, embedding_job_id),
                "error": "targeted visual embedding did not complete",
            }

    with SessionLocal() as db:
        after = counts_for_media(db, media_id=media_id, model_id=model_id, dimension=dimension)
    scratch_final = scratch_bytes()
    complete = (
        after["languageTraces"] > 0
        and after["visualTraces"] > 0
        and after["exactGenerationVisualEmbeddings"] == after["visualTraces"]
        and scratch_final <= scratch_initial
    )
    return complete, {
        **target,
        "before": before,
        "afterExtraction": after_extraction,
        "after": after,
        "extraction": safe_job_payload(extraction_status, extraction_job, extraction_job_id),
        "embedding": safe_job_payload(embedding_status, embedding_job, embedding_job_id),
        "scratchBytesAfterTarget": scratch_final,
        "complete": complete,
    }


def main() -> int:
    started = time.monotonic()
    scratch_initial = scratch_bytes()
    backend = QwenSubprocessEmbeddingBackend()

    ok_snapshot, snapshot_payload = queue_snapshot()
    if not ok_snapshot:
        return emit({"summary": "Retrieval coverage expansion could not read the live indexing queue"}, 1)
    snapshot = snapshot_payload.get("snapshot", {})
    worker = snapshot.get("worker") if isinstance(snapshot, dict) else None
    counts = snapshot.get("counts", {}) if isinstance(snapshot, dict) else {}
    worker_present = isinstance(worker, dict) and bool(worker.get("owner_id"))
    busy_jobs = sum(int(counts.get(state, 0) or 0) for state in ("queued", "running"))
    if not worker_present:
        return emit({"summary": "Retrieval coverage expansion requires the live singleton indexer worker"}, 1)
    if busy_jobs:
        return emit(
            {
                "summary": "Retrieval coverage expansion refused to compete with existing indexing work",
                "queuedOrRunningJobs": busy_jobs,
            },
            1,
        )

    results: list[dict[str, Any]] = []
    all_complete = True
    for target in TARGETS:
        complete, result = process_target(
            target=target,
            model_id=backend.model_id,
            dimension=backend.dimension,
            scratch_initial=scratch_initial,
        )
        results.append(result)
        if not complete:
            all_complete = False
            break

    scratch_final = scratch_bytes()
    scratch_clean = scratch_final <= scratch_initial
    ok = all_complete and len(results) == len(TARGETS) and scratch_clean
    return emit(
        {
            "summary": (
                "Targeted retrieval coverage expansion completed"
                if ok
                else "Targeted retrieval coverage expansion did not complete"
            ),
            "targets": results,
            "targetCount": len(TARGETS),
            "completedTargetCount": sum(1 for result in results if result.get("complete") is True),
            "modelId": backend.model_id,
            "dimension": backend.dimension,
            "scratch": {
                "initialBytes": scratch_initial,
                "finalBytes": scratch_final,
                "clean": scratch_clean,
            },
            "workerPresent": True,
            "bulkRemoteIndexingUsed": False,
            "metadataUsedForSelectionOrScoring": False,
            "durationSeconds": round(time.monotonic() - started, 3),
        },
        0 if ok else 1,
    )


if __name__ == "__main__":
    raise SystemExit(main())
