from __future__ import annotations

import json
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select, update

from app.core.config import ROOT_DIR, get_settings
from app.core.database import SessionLocal
from app.indexing.embedding_service import index_visual_trace_embeddings
from app.indexing.qwen_backend import QwenSubprocessEmbeddingBackend
from app.indexing.service import VisualExtractionConfig, index_all_visual_media
from app.indexing.stream_media import sync_all_stream_media
from app.library_models import Embedding, IndexRun, Media, Trace
from app.models import (
    AnalysisRun,
    CandidateWindow,
    DerivedAsset,
    PerformanceRecord,
    PublishingRecord,
    Stream,
    StreamAnalysisArtifact,
    StreamTranscript,
)
from app.services.clips_native_ask import NATIVE_ASK_MODEL_PREFIX, run_clips_native_ask
from app.services.library import ingest_local_media

TARGET_REGRESSION_VIDEO_ID = "pDC14ymQqWY"
FORBIDDEN_STALE_TITLE = "Studio Tour and Finished Pieces"


def emit(payload: dict[str, Any], code: int = 0) -> int:
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    return code


def sqlite_database_path(database_url: str) -> Path:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        raise RuntimeError("Derived-data reinitialization currently requires the configured SQLite database")
    raw = database_url[len(prefix) :]
    if not raw or raw == ":memory:":
        raise RuntimeError("Derived-data reinitialization requires a persistent SQLite database")
    path = Path(raw)
    if not path.is_absolute():
        path = (ROOT_DIR / path).resolve()
    return path


def row_count(db, model, *criteria) -> int:
    query = select(func.count()).select_from(model)
    if criteria:
        query = query.where(*criteria)
    return int(db.scalar(query) or 0)


def state_counts(db) -> dict[str, int]:
    return {
        "streams": row_count(db, Stream),
        "streamTranscripts": row_count(db, StreamTranscript),
        "media": row_count(db, Media),
        "analysisRuns": row_count(db, AnalysisRun),
        "nativeAnalysisRuns": row_count(db, AnalysisRun, AnalysisRun.model.like(f"{NATIVE_ASK_MODEL_PREFIX}%")),
        "directOrLegacyAnalysisRuns": row_count(db, AnalysisRun, ~AnalysisRun.model.like(f"{NATIVE_ASK_MODEL_PREFIX}%")),
        "candidateWindows": row_count(db, CandidateWindow),
        "streamAnalysisArtifacts": row_count(db, StreamAnalysisArtifact),
        "traces": row_count(db, Trace),
        "languageTraces": row_count(db, Trace, Trace.trace_type == "language"),
        "visualTraces": row_count(db, Trace, Trace.trace_type == "visual"),
        "embeddings": row_count(db, Embedding),
        "indexRuns": row_count(db, IndexRun),
        "derivedAssets": row_count(db, DerivedAsset),
        "publishingRecords": row_count(db, PublishingRecord),
        "performanceRecords": row_count(db, PerformanceRecord),
    }


def protected_editorial_counts(counts: dict[str, int]) -> dict[str, int]:
    return {
        key: counts[key]
        for key in ("derivedAssets", "publishingRecords", "performanceRecords")
        if counts.get(key, 0)
    }


def backup_database(database_path: Path) -> str:
    if not database_path.is_file():
        raise RuntimeError("Configured SQLite database file does not exist")
    backup_dir = ROOT_DIR / "data" / "reinitialize_backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_dir / f"shocks_art_before_reinitialize_{stamp}.sqlite3"
    with sqlite3.connect(str(database_path)) as source, sqlite3.connect(str(backup_path)) as target:
        source.backup(target)
    if not backup_path.is_file() or backup_path.stat().st_size <= 0:
        raise RuntimeError("SQLite safety backup was not created")
    return backup_path.name


def clear_index_job_queue(queue_path: Path) -> dict[str, int]:
    if not queue_path.exists():
        return {"removedJobs": 0, "runningJobs": 0}
    connection = sqlite3.connect(str(queue_path), timeout=30, isolation_level=None)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("BEGIN IMMEDIATE")
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if "index_jobs" not in tables:
            connection.execute("COMMIT")
            return {"removedJobs": 0, "runningJobs": 0}
        running = int(connection.execute("SELECT COUNT(*) FROM index_jobs WHERE status='running'").fetchone()[0])
        if running:
            connection.execute("ROLLBACK")
            raise RuntimeError(f"Refusing destructive reset while {running} indexing job(s) are running")
        total = int(connection.execute("SELECT COUNT(*) FROM index_jobs").fetchone()[0])
        connection.execute("DELETE FROM index_jobs")
        if "index_worker_lease" in tables:
            connection.execute("DELETE FROM index_worker_lease")
        connection.execute("COMMIT")
        return {"removedJobs": total, "runningJobs": 0}
    except Exception:
        try:
            connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        connection.close()


def clear_native_job_files(stream_ids: list[str]) -> int:
    job_dir = ROOT_DIR / "data" / "native_youtube_jobs"
    removed = 0
    for stream_id in stream_ids:
        for suffix in (
            ".json",
            ".primary.prompt.txt",
            ".primary.response.txt",
            ".fallback.prompt.txt",
            ".fallback.response.txt",
        ):
            path = job_dir / f"{stream_id}{suffix}"
            try:
                if path.is_file():
                    path.unlink()
                    removed += 1
            except OSError:
                pass
    return removed


def clear_visual_artifacts(index_root: Path) -> bool:
    visual_root = index_root.resolve() / "visual"
    if visual_root.is_dir():
        shutil.rmtree(visual_root)
        return True
    return False


def clear_derived_database_state(db) -> None:
    # Order is explicit so the reset is safe even when SQLite FK enforcement is enabled.
    db.execute(delete(StreamAnalysisArtifact))
    db.execute(delete(CandidateWindow))
    db.execute(delete(AnalysisRun))
    db.execute(delete(Embedding))
    db.execute(delete(Trace))
    db.execute(delete(IndexRun))
    db.execute(update(Stream).values(processing_status="queued"))
    db.commit()


def main() -> int:
    settings = get_settings()
    try:
        database_path = sqlite_database_path(settings.database_url)
        queue_path = Path(os.getenv("SHOCKS_INDEX_JOB_DB", str(ROOT_DIR / "data" / "indexing_jobs.sqlite3")))
        if not queue_path.is_absolute():
            queue_path = (ROOT_DIR / queue_path).resolve()

        with SessionLocal() as db:
            before = state_counts(db)
            protected = protected_editorial_counts(before)
            if protected:
                return emit(
                    {
                        "summary": "Refusing reinitialization because downstream publishing/editorial records exist",
                        "protected_counts": protected,
                        "before": before,
                    },
                    3,
                )
            stream_ids = list(db.scalars(select(Stream.stream_id)).all())
            native_stream_ids = list(
                db.scalars(
                    select(AnalysisRun.stream_id)
                    .where(
                        AnalysisRun.status == "complete",
                        AnalysisRun.model.like(f"{NATIVE_ASK_MODEL_PREFIX}%"),
                    )
                    .distinct()
                ).all()
            )
            regression_stream = db.scalar(select(Stream).where(Stream.source_video_id == TARGET_REGRESSION_VIDEO_ID))
            if regression_stream is not None and regression_stream.stream_id not in native_stream_ids:
                native_stream_ids.append(regression_stream.stream_id)

        queue_reset = clear_index_job_queue(queue_path)
        backup_name = backup_database(database_path)
        removed_native_job_files = clear_native_job_files(stream_ids)
        visual_artifacts_cleared = clear_visual_artifacts(Path(settings.library_index_path))

        with SessionLocal() as db:
            clear_derived_database_state(db)
            local_ingest = ingest_local_media(db, Path(settings.library_ingest_path))
            stream_sync = sync_all_stream_media(db, import_language=True)

            visual_results = index_all_visual_media(
                db,
                index_root=Path(settings.library_index_path),
                config=VisualExtractionConfig(),
                include_remote=False,
            )
            visual_failures = [result.as_dict() for result in visual_results if result.status != "complete"]
            if visual_failures:
                return emit(
                    {
                        "summary": "Derived reset completed, but local visual reindex did not complete cleanly",
                        "backup_file": backup_name,
                        "before": before,
                        "local_ingest": local_ingest.as_dict(),
                        "stream_sync": stream_sync.as_dict(),
                        "visual_failures": visual_failures[:10],
                    },
                    1,
                )

            embedding_result = index_visual_trace_embeddings(
                db,
                index_root=Path(settings.library_index_path),
                backend=QwenSubprocessEmbeddingBackend(),
            )

        clip_results: list[dict[str, Any]] = []
        clip_failures: list[dict[str, str]] = []
        for stream_id in native_stream_ids:
            result = run_clips_native_ask(stream_id)
            if result.get("status") != "complete":
                clip_failures.append({"stream_id": stream_id, "message": str(result.get("message") or "native Ask failed")[:300]})
                continue
            clip_results.append(
                {
                    "stream_id": stream_id,
                    "analysis_run_id": str(result.get("analysis_run_id") or ""),
                    "candidate_count": len(result.get("candidate_window_ids") or []),
                }
            )

        with SessionLocal() as db:
            after = state_counts(db)
            direct_after = after["directOrLegacyAnalysisRuns"]
            stale_title_count = row_count(
                db,
                CandidateWindow,
                func.lower(CandidateWindow.title) == FORBIDDEN_STALE_TITLE.lower(),
            )
            regression_candidates: list[dict[str, Any]] = []
            if regression_stream is not None:
                regression_candidates = [
                    {
                        "title": candidate.title,
                        "start_timestamp": candidate.start_timestamp,
                        "end_timestamp": candidate.end_timestamp,
                    }
                    for candidate in db.scalars(
                        select(CandidateWindow)
                        .where(CandidateWindow.stream_id == regression_stream.stream_id)
                        .order_by(CandidateWindow.candidate_rank)
                    ).all()
                ]

        failures: list[str] = []
        if direct_after != 0:
            failures.append(f"legacy/direct Gemini AnalysisRuns remain after reset: {direct_after}")
        if stale_title_count:
            failures.append(f"stale forbidden candidate title remains after reset: {stale_title_count}")
        if stream_sync.transcript_errors:
            failures.append(f"{stream_sync.transcript_errors} stored transcript(s) could not be rebuilt into Language Traces")
        if clip_failures:
            failures.append(f"{len(clip_failures)} previously-native stream(s) failed native-Ask reseeding")
        if regression_stream is not None and not regression_candidates:
            failures.append("Fractal Burning regression stream has no fresh candidate windows after reinitialization")

        payload: dict[str, Any] = {
            "summary": "Derived prototype data reinitialized from canonical source records",
            "backup_file": backup_name,
            "before": before,
            "after": after,
            "queue_reset": queue_reset,
            "native_job_files_removed": removed_native_job_files,
            "visual_artifacts_cleared": visual_artifacts_cleared,
            "local_ingest": local_ingest.as_dict(),
            "stream_sync": stream_sync.as_dict(),
            "visual_indexed_media": len(visual_results),
            "embedding_result": embedding_result.as_dict(),
            "native_streams_reseeded": clip_results,
            "native_stream_failures": clip_failures,
            "regression_candidates": regression_candidates,
            "protected_source_state": {
                "streams": after["streams"],
                "streamTranscripts": after["streamTranscripts"],
                "media": after["media"],
            },
        }
        if failures:
            payload["summary"] = "; ".join(failures)
            payload["failures"] = failures
            return emit(payload, 1)
        return emit(payload)
    except Exception as exc:
        return emit({"summary": f"Derived-data reinitialization failed: {type(exc).__name__}: {exc}"}, 1)


if __name__ == "__main__":
    raise SystemExit(main())
