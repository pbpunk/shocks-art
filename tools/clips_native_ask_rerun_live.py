from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TARGET_VIDEO_ID = "pDC14ymQqWY"


def emit(payload: dict[str, Any], code: int = 0) -> int:
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    return code


def safe_failure_job(job: dict[str, Any]) -> dict[str, Any]:
    return {
        key: job.get(key)
        for key in ("status", "message", "attempt", "returncode", "stderr", "stdout", "response_preview")
        if job.get(key) not in (None, "")
    }


def main() -> int:
    from sqlalchemy import func, select

    from app.core.database import SessionLocal
    from app.models import AnalysisRun, CandidateWindow, Stream
    from app.services.clips_native_ask import CLIPS_NATIVE_ASK_SOURCE, NATIVE_ASK_MODEL_PREFIX, run_clips_native_ask
    from app.services.native_automation import read_native_job_status

    with SessionLocal() as db:
        stream = db.scalar(select(Stream).where(Stream.source_video_id == TARGET_VIDEO_ID))
        if stream is None:
            return emit({"summary": f"Target YouTube stream {TARGET_VIDEO_ID} is not present in the production database"}, 2)
        stream_id = stream.stream_id
        title = stream.title
        before_direct = int(db.scalar(select(func.count()).select_from(AnalysisRun).where(AnalysisRun.stream_id == stream_id, ~AnalysisRun.model.like(f"{NATIVE_ASK_MODEL_PREFIX}%"))) or 0)
        before_native = int(db.scalar(select(func.count()).select_from(AnalysisRun).where(AnalysisRun.stream_id == stream_id, AnalysisRun.model.like(f"{NATIVE_ASK_MODEL_PREFIX}%"))) or 0)

    result = run_clips_native_ask(stream_id)
    if result.get("status") != "complete":
        return emit({
            "summary": f"YouTube Ask rerun failed: {result.get('message', 'unknown failure')}",
            "source_video_id": TARGET_VIDEO_ID,
            "job": safe_failure_job(read_native_job_status(stream_id)),
        }, 1)

    run_id = str(result.get("analysis_run_id") or "")
    candidate_ids = [str(value) for value in result.get("candidate_window_ids") or []]
    with SessionLocal() as db:
        run = db.get(AnalysisRun, run_id)
        candidates = list(db.scalars(select(CandidateWindow).where(CandidateWindow.analysis_run_id == run_id).order_by(CandidateWindow.candidate_rank)).all())
        after_direct = int(db.scalar(select(func.count()).select_from(AnalysisRun).where(AnalysisRun.stream_id == stream_id, ~AnalysisRun.model.like(f"{NATIVE_ASK_MODEL_PREFIX}%"))) or 0)
        after_native = int(db.scalar(select(func.count()).select_from(AnalysisRun).where(AnalysisRun.stream_id == stream_id, AnalysisRun.model.like(f"{NATIVE_ASK_MODEL_PREFIX}%"))) or 0)

    failures: list[str] = []
    if run is None or run.status != "complete" or run.model != CLIPS_NATIVE_ASK_SOURCE:
        failures.append("fresh native-Ask AnalysisRun lineage is invalid")
    if after_direct != before_direct:
        failures.append(f"direct-Gemini run count changed from {before_direct} to {after_direct}")
    if after_native != before_native + 1:
        failures.append(f"expected exactly one fresh native-Ask run, observed {before_native} -> {after_native}")
    if not candidate_ids or set(candidate_ids) != {candidate.candidate_window_id for candidate in candidates}:
        failures.append("fresh candidate IDs do not match persisted rerun candidates")

    payload = {
        "summary": f"Reran {TARGET_VIDEO_ID} through native YouTube Ask",
        "source_video_id": TARGET_VIDEO_ID,
        "stream_id": stream_id,
        "stream_title": title,
        "analysis_run_id": run_id,
        "candidate_window_ids": candidate_ids,
        "direct_runs_before": before_direct,
        "direct_runs_after": after_direct,
        "native_runs_before": before_native,
        "native_runs_after": after_native,
        "candidates": [
            {
                "candidate_window_id": candidate.candidate_window_id,
                "rank": candidate.candidate_rank,
                "title": candidate.title,
                "start_timestamp": candidate.start_timestamp,
                "end_timestamp": candidate.end_timestamp,
                "summary": candidate.concise_summary,
            }
            for candidate in candidates
        ],
    }
    if failures:
        payload["summary"] = "; ".join(failures)
        payload["failures"] = failures
        return emit(payload, 1)
    return emit(payload)


if __name__ == "__main__":
    raise SystemExit(main())
