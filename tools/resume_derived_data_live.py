from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.indexing.embedding_service import index_visual_trace_embeddings
from app.indexing.qwen_backend import QwenSubprocessEmbeddingBackend
from app.library_models import Embedding, Trace
from app.models import AnalysisRun, CandidateWindow, Stream
from app.services.clips_native_ask import NATIVE_ASK_MODEL_PREFIX, run_clips_native_ask
from tools.reinitialize_derived_data_live import (
    BACKUP_DIR,
    NATIVE_RESEED_CHECKPOINT,
    TARGET_REGRESSION_VIDEO_ID,
    load_native_reseed_checkpoint,
    recover_native_reseed_stream_ids_from_backups,
)


def emit(payload: dict[str, Any], code: int = 0) -> int:
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    return code


def count(db, model, *criteria) -> int:
    query = select(func.count()).select_from(model)
    if criteria:
        query = query.where(*criteria)
    return int(db.scalar(query) or 0)


def resolve_resume_targets(db) -> tuple[list[str], str | None]:
    valid_stream_ids = set(db.scalars(select(Stream.stream_id)).all())
    regression_stream_id = db.scalar(
        select(Stream.stream_id).where(Stream.source_video_id == TARGET_REGRESSION_VIDEO_ID)
    )

    checkpoint = load_native_reseed_checkpoint(NATIVE_RESEED_CHECKPOINT)
    source = "checkpoint"
    if not checkpoint:
        checkpoint = recover_native_reseed_stream_ids_from_backups(BACKUP_DIR, valid_stream_ids)
        source = "safety_backup"

    targets = [stream_id for stream_id in dict.fromkeys(checkpoint) if stream_id in valid_stream_ids]
    if regression_stream_id is not None and regression_stream_id not in targets:
        targets.append(regression_stream_id)
    return targets, source if checkpoint else "regression_only"


def main() -> int:
    settings = get_settings()
    try:
        with SessionLocal() as db:
            language_traces = count(db, Trace, Trace.trace_type == "language")
            visual_traces = count(db, Trace, Trace.trace_type == "visual")
            if language_traces <= 0 or visual_traces <= 0:
                return emit(
                    {
                        "summary": "Refusing resume because the existing trace corpus is incomplete",
                        "language_traces": language_traces,
                        "visual_traces": visual_traces,
                    },
                    3,
                )

            targets, target_source = resolve_resume_targets(db)
            completed = set(
                db.scalars(
                    select(AnalysisRun.stream_id)
                    .where(
                        AnalysisRun.status == "complete",
                        AnalysisRun.model.like(f"{NATIVE_ASK_MODEL_PREFIX}%"),
                    )
                    .distinct()
                ).all()
            )
            pending = [stream_id for stream_id in targets if stream_id not in completed]
            before = {
                "languageTraces": language_traces,
                "visualTraces": visual_traces,
                "embeddings": count(db, Embedding),
                "candidateWindows": count(db, CandidateWindow),
                "nativeAnalysisRuns": count(
                    db,
                    AnalysisRun,
                    AnalysisRun.model.like(f"{NATIVE_ASK_MODEL_PREFIX}%"),
                ),
                "directOrLegacyAnalysisRuns": count(
                    db,
                    AnalysisRun,
                    ~AnalysisRun.model.like(f"{NATIVE_ASK_MODEL_PREFIX}%"),
                ),
            }

        clip_results: list[dict[str, Any]] = []
        clip_failures: list[dict[str, str]] = []
        for stream_id in pending:
            result = run_clips_native_ask(stream_id)
            if result.get("status") != "complete":
                clip_failures.append(
                    {
                        "stream_id": stream_id,
                        "message": str(result.get("message") or "native Ask failed")[:400],
                    }
                )
                continue
            clip_results.append(
                {
                    "stream_id": stream_id,
                    "analysis_run_id": str(result.get("analysis_run_id") or ""),
                    "candidate_count": len(result.get("candidate_window_ids") or []),
                }
            )

        embedding_result = None
        embedding_failure = ""
        try:
            with SessionLocal() as db:
                embedding_result = index_visual_trace_embeddings(
                    db,
                    index_root=Path(settings.library_index_path),
                    backend=QwenSubprocessEmbeddingBackend(),
                )
        except Exception as exc:
            embedding_failure = f"{type(exc).__name__}: {exc}"

        with SessionLocal() as db:
            after = {
                "languageTraces": count(db, Trace, Trace.trace_type == "language"),
                "visualTraces": count(db, Trace, Trace.trace_type == "visual"),
                "embeddings": count(db, Embedding),
                "candidateWindows": count(db, CandidateWindow),
                "nativeAnalysisRuns": count(
                    db,
                    AnalysisRun,
                    AnalysisRun.model.like(f"{NATIVE_ASK_MODEL_PREFIX}%"),
                ),
                "directOrLegacyAnalysisRuns": count(
                    db,
                    AnalysisRun,
                    ~AnalysisRun.model.like(f"{NATIVE_ASK_MODEL_PREFIX}%"),
                ),
            }
            completed_after = set(
                db.scalars(
                    select(AnalysisRun.stream_id)
                    .where(
                        AnalysisRun.status == "complete",
                        AnalysisRun.model.like(f"{NATIVE_ASK_MODEL_PREFIX}%"),
                    )
                    .distinct()
                ).all()
            )

        unresolved = [stream_id for stream_id in targets if stream_id not in completed_after]
        failures: list[str] = []
        if after["languageTraces"] != before["languageTraces"]:
            failures.append("Language Trace count changed during non-destructive resume")
        if after["visualTraces"] != before["visualTraces"]:
            failures.append("Visual Trace count changed during non-destructive resume")
        if after["directOrLegacyAnalysisRuns"] != 0:
            failures.append("direct/legacy Gemini AnalysisRuns remain")
        if unresolved:
            failures.append(f"{len(unresolved)} native-Ask reseed target(s) remain incomplete")
        if embedding_failure:
            failures.append("Qwen embedding tail did not complete")

        payload: dict[str, Any] = {
            "summary": "Derived rebuild resumed without clearing existing Traces",
            "target_source": target_source,
            "native_reseed_target_count": len(targets),
            "already_completed_target_count": len([stream_id for stream_id in targets if stream_id in completed]),
            "pending_target_count": len(pending),
            "native_streams_reseeded": clip_results,
            "native_stream_failures": clip_failures,
            "unresolved_stream_ids": unresolved,
            "before": before,
            "after": after,
            "embedding_result": embedding_result.as_dict() if embedding_result is not None else None,
            "embedding_failure": embedding_failure,
        }
        if failures:
            payload["summary"] = "; ".join(failures)
            payload["failures"] = failures
            return emit(payload, 1)
        return emit(payload)
    except Exception as exc:
        return emit({"summary": f"Derived-data resume failed: {type(exc).__name__}: {exc}"}, 1)


if __name__ == "__main__":
    raise SystemExit(main())
