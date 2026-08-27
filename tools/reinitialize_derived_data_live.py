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
BACKUP_DIR = ROOT_DIR / "data" / "reinitialize_backups"
NATIVE_RESEED_CHECKPOINT = ROOT_DIR / "data" / "reinitialize_native_reseed_targets.json"


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


def native_reseed_stream_ids(db, regression_stream_id: str | None) -> list[str]:
    """Return native-Ask streams that must survive a clean rebuild or retry.

    A partial reinitialization may leave a stream with a failed native AnalysisRun after
    the original completed lineage has already been cleared. Including failed native
    lineage prevents a retry from silently shrinking the intended reseed corpus.
    """

    stream_ids = list(
        db.scalars(
            select(AnalysisRun.stream_id)
            .where(
                AnalysisRun.status.in_(("complete", "failed")),
                AnalysisRun.model.like(f"{NATIVE_ASK_MODEL_PREFIX}%"),
            )
            .distinct()
        ).all()
    )
    if regression_stream_id is not None and regression_stream_id not in stream_ids:
        stream_ids.append(regression_stream_id)
    return stream_ids


def load_native_reseed_checkpoint(path: Path = NATIVE_RESEED_CHECKPOINT) -> list[str]:
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    values = payload.get("stream_ids") if isinstance(payload, dict) else None
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, str) and value]


def save_native_reseed_checkpoint(stream_ids: list[str], path: Path = NATIVE_RESEED_CHECKPOINT) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    unique = list(dict.fromkeys(stream_ids))
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "stream_ids": unique,
            },
            separators=(",", ":"),
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def remove_native_reseed_checkpoint(path: Path = NATIVE_RESEED_CHECKPOINT) -> None:
    try:
        if path.is_file():
            path.unlink()
    except OSError:
        pass


def recover_native_reseed_stream_ids_from_backups(
    backup_dir: Path,
    valid_stream_ids: set[str],
) -> list[str]:
    """Recover the most recent pre-reset native lineage after an early reset failure."""

    if not backup_dir.is_dir():
        return []
    backups = sorted(
        (path for path in backup_dir.glob("shocks_art_before_reinitialize_*.sqlite3") if path.is_file()),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    for backup in backups:
        try:
            with sqlite3.connect(f"file:{backup}?mode=ro", uri=True) as connection:
                tables = {
                    str(row[0])
                    for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                }
                if "analysis_runs" not in tables:
                    continue
                rows = connection.execute(
                    "SELECT DISTINCT stream_id FROM analysis_runs "
                    "WHERE status IN ('complete','failed') AND model LIKE ?",
                    (f"{NATIVE_ASK_MODEL_PREFIX}%",),
                ).fetchall()
        except sqlite3.Error:
            continue
        recovered = [str(row[0]) for row in rows if str(row[0]) in valid_stream_ids]
        if recovered:
            return recovered
    return []


def resolve_native_reseed_stream_ids(
    db,
    regression_stream_id: str | None,
    valid_stream_ids: set[str],
    *,
    checkpoint_path: Path = NATIVE_RESEED_CHECKPOINT,
    backup_dir: Path = BACKUP_DIR,
) -> list[str]:
    current_native = native_reseed_stream_ids(db, None)
    if current_native:
        targets = current_native
    else:
        targets = load_native_reseed_checkpoint(checkpoint_path)
        if not targets:
            targets = recover_native_reseed_stream_ids_from_backups(backup_dir, valid_stream_ids)
    targets = [stream_id for stream_id in dict.fromkeys(targets) if stream_id in valid_stream_ids]
    if regression_stream_id is not None and regression_stream_id not in targets:
        targets.append(regression_stream_id)
    return targets


def backup_database(database_path: Path) -> str:
    if not database_path.is_file():
        raise RuntimeError("Configured SQLite database file does not exist")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = BACKUP_DIR / f"shocks_art_before_reinitialize_{stamp}.sqlite3"
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
    db.execute(delete(StreamAnalysisArtifact))
    db.execute(delete(CandidateWindow))
    db.execute(delete(AnalysisRun))
    db.execute(delete(Embedding))
    db.execute(delete(Trace))
    db.execute(delete(IndexRun))
    db.execute(delete(Media))
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
            valid_stream_ids = set(stream_ids)
            regression_stream_id = db.scalar(
                select(Stream.stream_id).where(Stream.source_video_id == TARGET_REGRESSION_VIDEO_ID)
            )
            native_stream_ids = resolve_native_reseed_stream_ids(
                db,
                regression_stream_id,
                valid_stream_ids,
            )
            save_native_reseed_checkpoint(native_stream_ids)

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
                        "native_reseed_target_count": len(native_stream_ids),
                        "visual_failures": visual_failures[:10],
                    },
                    1,
                )

        # Clips depend on the canonical stream/transcript rebuild, not on Qwen visual
        # embeddings. Restore the creator-facing production feed before the optional
        # visual-embedding tail so a GPU timeout cannot leave Clips empty.
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
            after = state_counts(db)
            direct_after = after["directOrLegacyAnalysisRuns"]
            stale_title_count = row_count(
                db,
                CandidateWindow,
                func.lower(CandidateWindow.title) == FORBIDDEN_STALE_TITLE.lower(),
            )
            regression_candidates: list[dict[str, Any]] = []
            if regression_stream_id is not None:
                regression_candidates = [
                    {
                        "title": candidate.title,
                        "start_timestamp": candidate.start_timestamp,
                        "end_timestamp": candidate.end_timestamp,
                    }
                    for candidate in db.scalars(
                        select(CandidateWindow)
                        .where(CandidateWindow.stream_id == regression_stream_id)
                        .order_by(CandidateWindow.candidate_rank)
                    ).all()
                ]

        failures: list[str] = []
        if after["streams"] != before["streams"]:
            failures.append(f"canonical Stream count changed during reset: {before['streams']} -> {after['streams']}")
        if after["streamTranscripts"] != before["streamTranscripts"]:
            failures.append(
                f"canonical StreamTranscript count changed during reset: {before['streamTranscripts']} -> {after['streamTranscripts']}"
            )
        if direct_after != 0:
            failures.append(f"legacy/direct Gemini AnalysisRuns remain after reset: {direct_after}")
        if stale_title_count:
            failures.append(f"stale forbidden candidate title remains after reset: {stale_title_count}")
        if stream_sync.transcript_errors:
            failures.append(f"{stream_sync.transcript_errors} stored transcript(s) could not be rebuilt into Language Traces")
        if clip_failures:
            failures.append(f"{len(clip_failures)} previously-native stream(s) failed native-Ask reseeding")
        if regression_stream_id is not None and not regression_candidates:
            failures.append("Fractal Burning regression stream has no fresh candidate windows after reinitialization")
        if embedding_failure:
            failures.append(f"visual embedding rebuild failed: {embedding_failure}")

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
            "embedding_result": (
                embedding_result.as_dict()
                if embedding_result is not None
                else {"error": embedding_failure, "persisted": after["embeddings"]}
            ),
            "native_reseed_target_count": len(native_stream_ids),
            "native_streams_reseeded": clip_results,
            "native_stream_failures": clip_failures,
            "regression_candidates": regression_candidates,
            "canonical_source_records": {
                "streams_before": before["streams"],
                "streams_after": after["streams"],
                "streamTranscripts_before": before["streamTranscripts"],
                "streamTranscripts_after": after["streamTranscripts"],
                "rebuiltMedia": after["media"],
            },
        }
        if failures:
            payload["summary"] = "; ".join(failures)
            payload["failures"] = failures
            return emit(payload, 1)

        remove_native_reseed_checkpoint()
        return emit(payload)
    except Exception as exc:
        return emit({"summary": f"Derived-data reinitialization failed: {type(exc).__name__}: {exc}"}, 1)


if __name__ == "__main__":
    raise SystemExit(main())
