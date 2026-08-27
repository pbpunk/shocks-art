from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tools.reinitialize_derived_data_live import (
    clear_index_job_queue,
    protected_editorial_counts,
    sqlite_database_path,
)


def test_reinitialize_requires_persistent_sqlite() -> None:
    assert sqlite_database_path("sqlite:///./data/test.db").name == "test.db"
    with pytest.raises(RuntimeError, match="SQLite"):
        sqlite_database_path("postgresql://example/db")
    with pytest.raises(RuntimeError, match="persistent"):
        sqlite_database_path("sqlite:///:memory:")


def test_protected_editorial_state_fails_closed() -> None:
    assert protected_editorial_counts(
        {"derivedAssets": 0, "publishingRecords": 0, "performanceRecords": 0}
    ) == {}
    assert protected_editorial_counts(
        {"derivedAssets": 2, "publishingRecords": 1, "performanceRecords": 0}
    ) == {"derivedAssets": 2, "publishingRecords": 1}


def _queue(path: Path, rows: list[tuple[str, str]]) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE index_jobs (job_id TEXT PRIMARY KEY, status TEXT NOT NULL);
            CREATE TABLE index_worker_lease (
                worker_key TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                lease_expires_at TEXT NOT NULL,
                heartbeat_at TEXT NOT NULL
            );
            """
        )
        connection.executemany("INSERT INTO index_jobs(job_id,status) VALUES (?,?)", rows)
        connection.execute(
            "INSERT INTO index_worker_lease VALUES ('library-indexer','worker','2099-01-01T00:00:00Z','2099-01-01T00:00:00Z')"
        )


def test_queue_reset_clears_nonrunning_jobs_but_keeps_worker_lease(tmp_path: Path) -> None:
    path = tmp_path / "jobs.sqlite3"
    _queue(path, [("one", "queued"), ("two", "completed")])
    assert clear_index_job_queue(path) == {"removedJobs": 2, "runningJobs": 0}
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM index_jobs").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM index_worker_lease").fetchone()[0] == 1


def test_queue_reset_refuses_running_job(tmp_path: Path) -> None:
    path = tmp_path / "jobs.sqlite3"
    _queue(path, [("one", "running")])
    with pytest.raises(RuntimeError, match="running"):
        clear_index_job_queue(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM index_jobs").fetchone()[0] == 1
