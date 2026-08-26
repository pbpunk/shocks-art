from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from app.indexing.job_queue import IndexJobQueue
from tools.indexer_worker_cleanup import clear_dead_owner


NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


def test_dead_worker_cleanup_releases_exact_pid_and_running_job(tmp_path: Path) -> None:
    queue_path = tmp_path / "jobs.sqlite3"
    queue = IndexJobQueue(queue_path)
    queue.enqueue("sync-stream-media", job_id="recover-after-stop", now=NOW)
    owner = "desktop:4321:abcdef12"
    assert queue.acquire_worker(owner, ttl_seconds=90, now=NOW)
    claimed = queue.claim_next(owner, ttl_seconds=90, now=NOW)
    assert claimed is not None and claimed.status == "running"

    mismatch = clear_dead_owner(9999, queue_path=queue_path)
    assert mismatch["released"] is False
    assert queue.snapshot()["worker"]["owner_id"] == owner

    cleared = clear_dead_owner(4321, queue_path=queue_path)
    assert cleared == {"released": True, "jobsReleased": 1, "reason": "dead-owner-cleared"}
    assert queue.snapshot()["worker"] is None
    assert queue.get("recover-after-stop").lease_expires_at == ""

    replacement = "desktop:5555:fedcba98"
    assert queue.acquire_worker(replacement, ttl_seconds=90, now=NOW)
    recovered = queue.claim_next(replacement, ttl_seconds=90, now=NOW)
    assert recovered is not None
    assert recovered.job_id == "recover-after-stop"
    assert recovered.attempt_count == 2


def test_app_lifecycle_owns_indexer_worker() -> None:
    start = (ROOT / "tools" / "start_app.ps1").read_text(encoding="utf-8")
    stop = (ROOT / "tools" / "stop_app.ps1").read_text(encoding="utf-8")
    start_worker = (ROOT / "tools" / "start_indexer_worker.ps1").read_text(encoding="utf-8")
    stop_worker = (ROOT / "tools" / "stop_indexer_worker.ps1").read_text(encoding="utf-8")

    assert 'start_indexer_worker.ps1' in start
    assert 'stop_indexer_worker.ps1' in stop
    assert 'app.indexing.worker' in start_worker
    assert 'indexer_worker.pid' in start_worker
    assert 'app.indexing.worker' in stop_worker
    assert 'indexer_worker_cleanup.py' in stop_worker
    assert "exit 0" not in start_worker
    assert "exit 0" not in stop_worker


def test_soak_profile_exercises_live_queue_restart_and_search() -> None:
    soak = (ROOT / "tools" / "host_profiles" / "indexer_soak.py").read_text(encoding="utf-8")
    assert '/api/library/indexing/jobs' in soak
    assert 'sync-stream-media' in soak
    assert 'stop_indexer_worker.ps1' in soak
    assert 'start_indexer_worker.ps1' in soak
    assert '/api/library/search/visual' in soak
    assert '/health' in soak
    assert 'nvidia-smi' in soak
    assert 'library_scratch' in soak
