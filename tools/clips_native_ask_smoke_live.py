from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PREFERRED_REGRESSION_VIDEO_ID = "pDC14ymQqWY"


def emit(payload: dict[str, Any], code: int = 0) -> int:
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    return code


def select_target_stream(streams: list[Any]) -> Any | None:
    for stream in streams:
        if stream.source_video_id == PREFERRED_REGRESSION_VIDEO_ID:
            return stream
    return streams[0] if streams else None


def safe_job_receipt(result: dict[str, Any]) -> dict[str, Any]:
    return {
        key: result.get(key)
        for key in ("status", "message", "source", "returncode", "attempt")
        if key in result
    }


def main() -> int:
    from sqlalchemy import func, select

    from app.core.database import SessionLocal
    from app.models import AnalysisRun, CandidateWindow
    from app.services.clips_native_ask import (
        CLIPS_NATIVE_ASK_SOURCE,
        NATIVE_ASK_MODEL_PREFIX,
        pending_native_ask_streams,
        production_candidate_ids,
        run_clips_native_ask,
    )

    def direct_run_count(db, stream_id: str) -> int:
        return int(
            db.scalar(
                select(func.count())
                .select_from(AnalysisRun)
                .where(
                    AnalysisRun.stream_id == stream_id,
                    ~AnalysisRun.model.like(f"{NATIVE_ASK_MODEL_PREFIX}%"),
                )
            )
            or 0
        )

    with SessionLocal() as db:
        target = select_target_stream(pending_native_ask_streams(db))
        if target is None:
            return emit(
                {
                    "summary": "No stream is pending native YouTube Ask, so the browser smoke could not exercise a real production analysis run",
                    "no_pending_stream": True,
                },
                3,
            )
        stream_id = target.stream_id
        source_video_id = target.source_video_id
        title = target.title
        preferred_target = source_video_id == PREFERRED_REGRESSION_VIDEO_ID
        before_direct_runs = direct_run_count(db, stream_id)

    result = run_clips_native_ask(stream_id)
    safe_job = safe_job_receipt(result)
    if result.get("status") != "complete":
        return emit(
            {
                "summary": f"YouTube Ask smoke failed for {source_video_id}: {result.get('message', 'unknown failure')}",
                "stream_id": stream_id,
                "source_video_id": source_video_id,
                "preferred_regression_target": preferred_target,
                "job": safe_job,
            },
            1,
        )

    run_id = str(result.get("analysis_run_id") or "")
    candidate_ids = [str(value) for value in result.get("candidate_window_ids") or []]
    if not run_id or not candidate_ids:
        return emit(
            {
                "summary": "YouTube Ask smoke completed without a durable AnalysisRun and at least one new candidate",
                "stream_id": stream_id,
                "source_video_id": source_video_id,
                "job": safe_job,
            },
            1,
        )

    with SessionLocal() as db:
        run = db.get(AnalysisRun, run_id)
        persisted_candidate_ids = set(
            db.scalars(
                select(CandidateWindow.candidate_window_id).where(
                    CandidateWindow.analysis_run_id == run_id,
                )
            ).all()
        )
        production_ids = production_candidate_ids(db)
        after_direct_runs = direct_run_count(db, stream_id)

    failures: list[str] = []
    if run is None:
        failures.append("AnalysisRun was not persisted")
    else:
        if run.status != "complete":
            failures.append(f"AnalysisRun status is {run.status!r}, expected 'complete'")
        if run.model != CLIPS_NATIVE_ASK_SOURCE:
            failures.append(f"AnalysisRun model is {run.model!r}, expected {CLIPS_NATIVE_ASK_SOURCE!r}")
    if not set(candidate_ids).issubset(persisted_candidate_ids):
        failures.append("job candidate IDs do not match persisted CandidateWindow lineage")
    if not set(candidate_ids).issubset(production_ids):
        failures.append("new native-Ask candidates are not eligible for the production Clips feed")
    if after_direct_runs != before_direct_runs:
        failures.append(
            f"direct-Gemini/non-native run count changed from {before_direct_runs} to {after_direct_runs}"
        )

    payload = {
        "summary": (
            f"Production YouTube Ask imported {len(candidate_ids)} candidate(s) from {source_video_id} without creating a direct-Gemini run"
        ),
        "stream_id": stream_id,
        "source_video_id": source_video_id,
        "stream_title": title,
        "preferred_regression_target": preferred_target,
        "analysis_run_id": run_id,
        "candidate_window_ids": candidate_ids,
        "source": result.get("source"),
        "direct_runs_before": before_direct_runs,
        "direct_runs_after": after_direct_runs,
    }
    if failures:
        payload["summary"] = "; ".join(failures)
        payload["failures"] = failures
        return emit(payload, 1)
    return emit(payload)


if __name__ == "__main__":
    raise SystemExit(main())
