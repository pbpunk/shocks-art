from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4


DEFAULT_QUEUE_PATH = Path(os.getenv("SHOCKS_INDEX_JOB_DB", "./data/indexing_jobs.sqlite3"))
DEFAULT_WORKER_KEY = "library-indexer"
ALLOWED_JOB_TYPES = frozenset({
    "visual-media",
    "visual-pending",
    "visual-embeddings",
    "sync-stream-media",
})
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


class IndexJobQueueError(RuntimeError):
    pass


@dataclass(frozen=True)
class IndexJob:
    job_id: str
    job_type: str
    media_id: str
    payload: dict[str, Any]
    status: str
    priority: int
    attempt_count: int
    max_attempts: int
    lease_owner: str
    lease_expires_at: str
    started_at: str
    completed_at: str
    error_message: str
    result: dict[str, Any]
    progress: dict[str, Any]
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "IndexJob":
        return cls(
            job_id=str(row["job_id"]),
            job_type=str(row["job_type"]),
            media_id=str(row["media_id"] or ""),
            payload=_decode_json(row["payload_json"]),
            status=str(row["status"]),
            priority=int(row["priority"]),
            attempt_count=int(row["attempt_count"]),
            max_attempts=int(row["max_attempts"]),
            lease_owner=str(row["lease_owner"] or ""),
            lease_expires_at=str(row["lease_expires_at"] or ""),
            started_at=str(row["started_at"] or ""),
            completed_at=str(row["completed_at"] or ""),
            error_message=str(row["error_message"] or ""),
            result=_decode_json(row["result_json"]),
            progress=_decode_json(row["progress_json"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )


def _decode_json(value: object) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _encode_json(value: dict[str, Any] | None) -> str:
    return json.dumps(value or {}, separators=(",", ":"), sort_keys=True)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    timestamp = (value or _utc_now()).astimezone(timezone.utc)
    return timestamp.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _expiry(now: datetime, ttl_seconds: int) -> str:
    return _iso(now + timedelta(seconds=ttl_seconds))


def _validate_ttl(ttl_seconds: int) -> int:
    ttl = int(ttl_seconds)
    if ttl < 5:
        raise ValueError("lease ttl must be at least 5 seconds")
    return ttl


def _validate_job_payload(job_type: str, payload: dict[str, Any] | None) -> dict[str, Any]:
    if job_type not in ALLOWED_JOB_TYPES:
        raise ValueError(f"unsupported indexing job type: {job_type}")
    value = dict(payload or {})
    allowed_fields = {
        "visual-media": {"sample_interval_seconds"},
        "visual-pending": {"sample_interval_seconds", "limit", "include_remote"},
        "visual-embeddings": {"limit"},
        "sync-stream-media": {"limit", "import_language"},
    }[job_type]
    unexpected = sorted(set(value) - allowed_fields)
    if unexpected:
        raise ValueError(f"unsupported payload fields for {job_type}: {', '.join(unexpected)}")
    if "sample_interval_seconds" in value and value["sample_interval_seconds"] is not None:
        interval = float(value["sample_interval_seconds"])
        if interval <= 0:
            raise ValueError("sample_interval_seconds must be positive")
        value["sample_interval_seconds"] = interval
    if "limit" in value and value["limit"] is not None:
        limit = int(value["limit"])
        if limit <= 0:
            raise ValueError("limit must be positive")
        value["limit"] = limit
    if "include_remote" in value:
        value["include_remote"] = bool(value["include_remote"])
    if "import_language" in value:
        value["import_language"] = bool(value["import_language"])
    return value


class IndexJobQueue:
    """Durable SQLite queue for the offline Library indexer.

    The queue intentionally owns no inference imports. It can be inspected or
    mutated by FastAPI later without loading Torch/Qwen/Whisper. A separate
    worker process claims fixed job types under a singleton lease.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or DEFAULT_QUEUE_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS index_jobs (
                    job_id TEXT PRIMARY KEY,
                    job_type TEXT NOT NULL,
                    media_id TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'queued',
                    priority INTEGER NOT NULL DEFAULT 100,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    lease_owner TEXT NOT NULL DEFAULT '',
                    lease_expires_at TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL DEFAULT '',
                    completed_at TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    result_json TEXT NOT NULL DEFAULT '{}',
                    progress_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK (attempt_count >= 0),
                    CHECK (max_attempts >= 1)
                );
                CREATE INDEX IF NOT EXISTS ix_index_jobs_status_priority_created
                    ON index_jobs(status, priority, created_at);
                CREATE TABLE IF NOT EXISTS index_worker_lease (
                    worker_key TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    lease_expires_at TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL
                );
                """
            )

    def enqueue(
        self,
        job_type: str,
        *,
        media_id: str = "",
        payload: dict[str, Any] | None = None,
        priority: int = 100,
        max_attempts: int = 3,
        job_id: str | None = None,
        now: datetime | None = None,
    ) -> IndexJob:
        clean_payload = _validate_job_payload(job_type, payload)
        media = str(media_id or "").strip()
        if job_type == "visual-media" and not media:
            raise ValueError("visual-media jobs require media_id")
        attempts = int(max_attempts)
        if attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        identifier = str(job_id or f"indexjob_{uuid4().hex}").strip()
        if not identifier:
            raise ValueError("job_id cannot be empty")
        timestamp = _iso(now)
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO index_jobs (
                    job_id, job_type, media_id, payload_json, status, priority,
                    attempt_count, max_attempts, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'queued', ?, 0, ?, ?, ?)
                """,
                (
                    identifier,
                    job_type,
                    media,
                    _encode_json(clean_payload),
                    int(priority),
                    attempts,
                    timestamp,
                    timestamp,
                ),
            )
        return self.get(identifier)

    def get(self, job_id: str) -> IndexJob:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM index_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise IndexJobQueueError(f"indexing job not found: {job_id}")
        return IndexJob.from_row(row)

    def list(self, *, status: str | None = None, limit: int = 100) -> list[IndexJob]:
        count = max(1, min(1000, int(limit)))
        with self._connect() as connection:
            if status:
                rows = connection.execute(
                    "SELECT * FROM index_jobs WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                    (status, count),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM index_jobs ORDER BY created_at DESC LIMIT ?", (count,)
                ).fetchall()
        return [IndexJob.from_row(row) for row in rows]

    def acquire_worker(
        self,
        owner_id: str,
        *,
        worker_key: str = DEFAULT_WORKER_KEY,
        ttl_seconds: int = 60,
        now: datetime | None = None,
    ) -> bool:
        owner = str(owner_id or "").strip()
        if not owner:
            raise ValueError("owner_id cannot be empty")
        ttl = _validate_ttl(ttl_seconds)
        current = now or _utc_now()
        current_iso = _iso(current)
        expires = _expiry(current, ttl)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT owner_id, lease_expires_at FROM index_worker_lease WHERE worker_key = ?",
                (worker_key,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO index_worker_lease(worker_key, owner_id, lease_expires_at, heartbeat_at) VALUES (?, ?, ?, ?)",
                    (worker_key, owner, expires, current_iso),
                )
                return True
            if str(row["owner_id"]) != owner and str(row["lease_expires_at"]) > current_iso:
                return False
            connection.execute(
                "UPDATE index_worker_lease SET owner_id = ?, lease_expires_at = ?, heartbeat_at = ? WHERE worker_key = ?",
                (owner, expires, current_iso, worker_key),
            )
            return True

    def heartbeat(
        self,
        owner_id: str,
        *,
        job_id: str = "",
        worker_key: str = DEFAULT_WORKER_KEY,
        ttl_seconds: int = 60,
        now: datetime | None = None,
    ) -> bool:
        ttl = _validate_ttl(ttl_seconds)
        current = now or _utc_now()
        current_iso = _iso(current)
        expires = _expiry(current, ttl)
        with self._transaction() as connection:
            result = connection.execute(
                """
                UPDATE index_worker_lease
                SET lease_expires_at = ?, heartbeat_at = ?
                WHERE worker_key = ? AND owner_id = ? AND lease_expires_at > ?
                """,
                (expires, current_iso, worker_key, owner_id, current_iso),
            )
            if result.rowcount != 1:
                return False
            if job_id:
                job_result = connection.execute(
                    """
                    UPDATE index_jobs
                    SET lease_expires_at = ?, updated_at = ?
                    WHERE job_id = ? AND status = 'running' AND lease_owner = ?
                    """,
                    (expires, current_iso, job_id, owner_id),
                )
                if job_result.rowcount != 1:
                    return False
            return True

    def release_worker(self, owner_id: str, *, worker_key: str = DEFAULT_WORKER_KEY) -> None:
        with self._transaction() as connection:
            connection.execute(
                "DELETE FROM index_worker_lease WHERE worker_key = ? AND owner_id = ?",
                (worker_key, owner_id),
            )

    def _recover_stale_locked(self, connection: sqlite3.Connection, current_iso: str) -> int:
        rows = connection.execute(
            """
            SELECT job_id, attempt_count, max_attempts
            FROM index_jobs
            WHERE status = 'running' AND (lease_expires_at = '' OR lease_expires_at <= ?)
            """,
            (current_iso,),
        ).fetchall()
        recovered = 0
        for row in rows:
            retryable = int(row["attempt_count"]) < int(row["max_attempts"])
            status = "queued" if retryable else "failed"
            completed_at = "" if retryable else current_iso
            message = "stale worker lease recovered" if retryable else "stale worker lease exhausted retries"
            connection.execute(
                """
                UPDATE index_jobs
                SET status = ?, lease_owner = '', lease_expires_at = '',
                    completed_at = ?, error_message = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (status, completed_at, message, current_iso, row["job_id"]),
            )
            recovered += 1
        return recovered

    def recover_stale_jobs(self, *, now: datetime | None = None) -> int:
        current_iso = _iso(now)
        with self._transaction() as connection:
            return self._recover_stale_locked(connection, current_iso)

    def claim_next(
        self,
        owner_id: str,
        *,
        worker_key: str = DEFAULT_WORKER_KEY,
        ttl_seconds: int = 60,
        now: datetime | None = None,
    ) -> IndexJob | None:
        ttl = _validate_ttl(ttl_seconds)
        current = now or _utc_now()
        current_iso = _iso(current)
        expires = _expiry(current, ttl)
        with self._transaction() as connection:
            lease = connection.execute(
                """
                SELECT 1 FROM index_worker_lease
                WHERE worker_key = ? AND owner_id = ? AND lease_expires_at > ?
                """,
                (worker_key, owner_id, current_iso),
            ).fetchone()
            if lease is None:
                raise IndexJobQueueError("worker lease is not held")
            self._recover_stale_locked(connection, current_iso)
            row = connection.execute(
                """
                SELECT job_id FROM index_jobs
                WHERE status = 'queued'
                ORDER BY priority ASC, created_at ASC, job_id ASC
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            job_id = str(row["job_id"])
            connection.execute(
                """
                UPDATE index_jobs
                SET status = 'running', attempt_count = attempt_count + 1,
                    lease_owner = ?, lease_expires_at = ?,
                    started_at = CASE WHEN started_at = '' THEN ? ELSE started_at END,
                    completed_at = '', updated_at = ?
                WHERE job_id = ? AND status = 'queued'
                """,
                (owner_id, expires, current_iso, current_iso, job_id),
            )
            claimed = connection.execute(
                "SELECT * FROM index_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return IndexJob.from_row(claimed) if claimed is not None else None

    def update_progress(self, job_id: str, owner_id: str, progress: dict[str, Any]) -> bool:
        timestamp = _iso()
        with self._transaction() as connection:
            result = connection.execute(
                """
                UPDATE index_jobs SET progress_json = ?, updated_at = ?
                WHERE job_id = ? AND status = 'running' AND lease_owner = ?
                """,
                (_encode_json(progress), timestamp, job_id, owner_id),
            )
            return result.rowcount == 1

    def complete(self, job_id: str, owner_id: str, result: dict[str, Any] | None = None) -> bool:
        timestamp = _iso()
        with self._transaction() as connection:
            updated = connection.execute(
                """
                UPDATE index_jobs
                SET status = 'completed', completed_at = ?, result_json = ?,
                    error_message = '', lease_owner = '', lease_expires_at = '', updated_at = ?
                WHERE job_id = ? AND status = 'running' AND lease_owner = ?
                """,
                (timestamp, _encode_json(result), timestamp, job_id, owner_id),
            )
            return updated.rowcount == 1

    def fail(
        self,
        job_id: str,
        owner_id: str,
        error: str,
        *,
        retryable: bool = True,
    ) -> str:
        timestamp = _iso()
        message = str(error or "indexing job failed")[:4000]
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT attempt_count, max_attempts FROM index_jobs
                WHERE job_id = ? AND status = 'running' AND lease_owner = ?
                """,
                (job_id, owner_id),
            ).fetchone()
            if row is None:
                raise IndexJobQueueError("cannot fail a job not owned by this worker")
            can_retry = retryable and int(row["attempt_count"]) < int(row["max_attempts"])
            status = "queued" if can_retry else "failed"
            completed_at = "" if can_retry else timestamp
            connection.execute(
                """
                UPDATE index_jobs
                SET status = ?, completed_at = ?, error_message = ?,
                    lease_owner = '', lease_expires_at = '', updated_at = ?
                WHERE job_id = ?
                """,
                (status, completed_at, message, timestamp, job_id),
            )
            return status

    def retry(self, job_id: str) -> IndexJob:
        timestamp = _iso()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT status FROM index_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise IndexJobQueueError(f"indexing job not found: {job_id}")
            if str(row["status"]) not in {"failed", "cancelled"}:
                raise IndexJobQueueError("only failed or cancelled jobs can be retried manually")
            connection.execute(
                """
                UPDATE index_jobs
                SET status = 'queued', attempt_count = 0, completed_at = '',
                    error_message = '', lease_owner = '', lease_expires_at = '', updated_at = ?
                WHERE job_id = ?
                """,
                (timestamp, job_id),
            )
        return self.get(job_id)

    def snapshot(self) -> dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM index_jobs GROUP BY status"
            ).fetchall()
            lease = connection.execute(
                "SELECT worker_key, owner_id, lease_expires_at, heartbeat_at FROM index_worker_lease WHERE worker_key = ?",
                (DEFAULT_WORKER_KEY,),
            ).fetchone()
        return {
            "counts": {str(row["status"]): int(row["count"]) for row in rows},
            "worker": dict(lease) if lease is not None else None,
        }
