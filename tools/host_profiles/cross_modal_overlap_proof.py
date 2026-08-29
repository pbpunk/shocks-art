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
from app.indexing.language_search import search_language_traces
from app.indexing.qwen_query_backend import QwenPersistentQueryEmbeddingBackend
from app.indexing.retrieval_fusion import fuse_temporal_retrieval
from app.indexing.visual_search import search_visual_embeddings
from app.library_models import Embedding, Media, Trace


BASE_URL = os.getenv("SHOCKS_CROSS_MODAL_PROOF_BASE_URL", "http://127.0.0.1:8000/shocks_art").rstrip("/")
TARGET_MEDIA_ID = "media_66612c0710ad4b8ba78e3653256af2fe"
QUERY = "sanding axes"
SCRATCH = Path(os.getenv("LIBRARY_SCRATCH_PATH", LIVE_ROOT / "data" / "library_scratch"))
JOB_TIMEOUT_SECONDS = 1500
TERMINAL_JOB_STATES = frozenset({"completed", "failed", "cancelled"})


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


def enqueue_job(job_type: str, *, media_id: str) -> tuple[str, dict[str, Any]]:
    status, payload = request_json(
        "/api/library/indexing/jobs",
        method="POST",
        payload={"job_type": job_type, "media_id": media_id},
    )
    if status != 200:
        return "", payload
    job = payload.get("job", {})
    return str(job.get("jobId") or ""), job if isinstance(job, dict) else {}


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


def _counts(db, *, model_id: str, dimension: int) -> dict[str, int]:
    language = int(
        db.scalar(
            select(func.count(Trace.trace_id)).where(
                Trace.media_id == TARGET_MEDIA_ID,
                Trace.trace_type == "language",
            )
        )
        or 0
    )
    visual = int(
        db.scalar(
            select(func.count(Trace.trace_id)).where(
                Trace.media_id == TARGET_MEDIA_ID,
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
                Trace.media_id == TARGET_MEDIA_ID,
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


def _safe_job_payload(status: str, job: dict[str, Any], job_id: str) -> dict[str, Any]:
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    return {
        "jobId": job_id or None,
        "status": status,
        "error": job.get("error") or None,
        "result": result,
    }


def _query_proof(db, backend: QwenPersistentQueryEmbeddingBackend) -> dict[str, Any]:
    query_started = time.perf_counter()
    vectors = backend.embed_text([QUERY])
    query_embedding_ms = (time.perf_counter() - query_started) * 1000.0
    if len(vectors) != 1:
        raise RuntimeError(f"Qwen returned {len(vectors)} vectors for the fixed overlap query")

    language = search_language_traces(db, query=QUERY, top_k=100)
    visual = search_visual_embeddings(
        db,
        query_vector=vectors[0],
        model_id=backend.model_id,
        dimension=backend.dimension,
        top_k=1000,
    )
    target_language = [match for match in language.matches if match.media_id == TARGET_MEDIA_ID]
    target_visual = [match for match in visual.matches if match.media_id == TARGET_MEDIA_ID]
    fused = fuse_temporal_retrieval(target_language, target_visual, top_k=5)

    language_rank = next(
        (rank for rank, match in enumerate(language.matches, start=1) if match.media_id == TARGET_MEDIA_ID),
        None,
    )
    visual_rank = next(
        (rank for rank, match in enumerate(visual.matches, start=1) if match.media_id == TARGET_MEDIA_ID),
        None,
    )
    return {
        "query": QUERY,
        "queryEmbeddingMs": round(query_embedding_ms, 4),
        "targetLanguageRank": language_rank,
        "targetVisualRank": visual_rank,
        "targetLanguageMatches": len(target_language),
        "targetVisualMatches": len(target_visual),
        "targetFusedMatches": len(fused),
        "fused": [
            {
                "startMs": match.start_ms,
                "endMs": match.end_ms,
                "gapMs": match.gap_ms,
                "languageRankWithinTarget": match.language_rank,
                "visualRankWithinTarget": match.visual_rank,
                "languageScore": round(match.language_score, 8),
                "visualScore": round(match.visual_score, 8),
            }
            for match in fused
        ],
        "metadataUsed": False,
    }


def main() -> int:
    started = time.monotonic()
    scratch_initial = scratch_bytes()
    backend = QwenPersistentQueryEmbeddingBackend()

    ok_snapshot, snapshot_payload = queue_snapshot()
    if not ok_snapshot:
        return emit({"summary": "Cross-modal proof could not read the live indexing queue"}, 1)
    snapshot = snapshot_payload.get("snapshot", {})
    worker = snapshot.get("worker") if isinstance(snapshot, dict) else None
    counts = snapshot.get("counts", {}) if isinstance(snapshot, dict) else {}
    worker_present = isinstance(worker, dict) and bool(worker.get("owner_id"))
    busy_jobs = sum(int(counts.get(state, 0) or 0) for state in ("queued", "running"))
    if not worker_present:
        return emit({"summary": "Cross-modal proof requires the live singleton indexer worker"}, 1)
    if busy_jobs:
        return emit(
            {
                "summary": "Cross-modal proof refused to compete with existing indexing work",
                "queuedOrRunningJobs": busy_jobs,
            },
            1,
        )

    with SessionLocal() as db:
        media = db.get(Media, TARGET_MEDIA_ID)
        if media is None:
            return emit({"summary": "Fixed cross-modal proof Media is not present"}, 1)
        if media.source_type != "youtube":
            return emit({"summary": "Fixed cross-modal proof Media is not canonical YouTube Media"}, 1)
        before = _counts(db, model_id=backend.model_id, dimension=backend.dimension)
    if before["languageTraces"] <= 0:
        return emit({"summary": "Fixed cross-modal proof Media has no Language Traces", "before": before}, 1)

    extraction_status = "already-present"
    extraction_job: dict[str, Any] = {}
    extraction_job_id = ""
    if before["visualTraces"] <= 0:
        extraction_job_id, _ = enqueue_job("visual-media", media_id=TARGET_MEDIA_ID)
        if not extraction_job_id:
            return emit({"summary": "Cross-modal proof could not enqueue targeted visual extraction"}, 1)
        extraction_status, extraction_job = wait_for_job(extraction_job_id)
        if extraction_status != "completed":
            return emit(
                {
                    "summary": "Targeted remote visual extraction did not complete",
                    "targetMediaId": TARGET_MEDIA_ID,
                    "extraction": _safe_job_payload(extraction_status, extraction_job, extraction_job_id),
                    "scratch": {"initialBytes": scratch_initial, "finalBytes": scratch_bytes()},
                },
                1,
            )

    with SessionLocal() as db:
        after_extraction = _counts(db, model_id=backend.model_id, dimension=backend.dimension)
    if after_extraction["visualTraces"] <= 0:
        return emit(
            {
                "summary": "Targeted extraction completed without producing visual Traces",
                "afterExtraction": after_extraction,
            },
            1,
        )

    embedding_status = "already-present"
    embedding_job: dict[str, Any] = {}
    embedding_job_id = ""
    if after_extraction["exactGenerationVisualEmbeddings"] < after_extraction["visualTraces"]:
        embedding_job_id, _ = enqueue_job("visual-embeddings", media_id=TARGET_MEDIA_ID)
        if not embedding_job_id:
            return emit({"summary": "Cross-modal proof could not enqueue targeted visual embeddings"}, 1)
        embedding_status, embedding_job = wait_for_job(embedding_job_id)
        if embedding_status != "completed":
            return emit(
                {
                    "summary": "Targeted visual embedding did not complete",
                    "targetMediaId": TARGET_MEDIA_ID,
                    "extraction": _safe_job_payload(extraction_status, extraction_job, extraction_job_id),
                    "embedding": _safe_job_payload(embedding_status, embedding_job, embedding_job_id),
                    "scratch": {"initialBytes": scratch_initial, "finalBytes": scratch_bytes()},
                },
                1,
            )

    with SessionLocal() as db:
        after = _counts(db, model_id=backend.model_id, dimension=backend.dimension)
        query_proof = _query_proof(db, backend)

    scratch_final = scratch_bytes()
    scratch_clean = scratch_final <= scratch_initial
    overlap_proven = (
        after["languageTraces"] > 0
        and after["visualTraces"] > 0
        and after["exactGenerationVisualEmbeddings"] > 0
        and query_proof["targetLanguageMatches"] > 0
        and query_proof["targetVisualMatches"] > 0
        and query_proof["targetFusedMatches"] > 0
    )
    ok = overlap_proven and scratch_clean
    return emit(
        {
            "summary": (
                "Targeted YouTube Media now has grounded cross-modal retrieval overlap"
                if ok
                else "Targeted cross-modal overlap proof did not satisfy acceptance"
            ),
            "targetMediaId": TARGET_MEDIA_ID,
            "sourceType": "youtube",
            "before": before,
            "afterExtraction": after_extraction,
            "after": after,
            "extraction": _safe_job_payload(extraction_status, extraction_job, extraction_job_id),
            "embedding": _safe_job_payload(embedding_status, embedding_job, embedding_job_id),
            "queryProof": query_proof,
            "scratch": {
                "initialBytes": scratch_initial,
                "finalBytes": scratch_final,
                "clean": scratch_clean,
            },
            "workerPresent": True,
            "metadataUsedForScoring": False,
            "durationSeconds": round(time.monotonic() - started, 3),
        },
        0 if ok else 1,
    )


if __name__ == "__main__":
    raise SystemExit(main())
