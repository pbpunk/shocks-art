from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.indexing.job_queue import IndexJobQueue, IndexJobQueueError


NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)


def queue(tmp_path: Path) -> IndexJobQueue:
    return IndexJobQueue(tmp_path / "jobs.sqlite3")


def test_queue_persists_claim_and_completion(tmp_path: Path) -> None:
    jobs = queue(tmp_path)
    created = jobs.enqueue(
        "visual-media",
        media_id="media_123",
        payload={"sample_interval_seconds": 5},
        job_id="job-one",
        now=NOW,
    )
    assert created.status == "queued"
    assert created.payload == {"sample_interval_seconds": 5.0}

    assert jobs.acquire_worker("worker-a", ttl_seconds=30, now=NOW)
    claimed = jobs.claim_next("worker-a", ttl_seconds=30, now=NOW)
    assert claimed is not None
    assert claimed.job_id == "job-one"
    assert claimed.status == "running"
    assert claimed.attempt_count == 1
    assert claimed.lease_owner == "worker-a"

    assert jobs.complete("job-one", "worker-a", {"created": 3})
    reopened = IndexJobQueue(tmp_path / "jobs.sqlite3")
    completed = reopened.get("job-one")
    assert completed.status == "completed"
    assert completed.result == {"created": 3}
    assert completed.lease_owner == ""


def test_singleton_worker_lease_blocks_second_owner_until_release(tmp_path: Path) -> None:
    jobs = queue(tmp_path)
    assert jobs.acquire_worker("worker-a", ttl_seconds=30, now=NOW)
    assert not jobs.acquire_worker("worker-b", ttl_seconds=30, now=NOW)
    jobs.release_worker("worker-a")
    assert jobs.acquire_worker("worker-b", ttl_seconds=30, now=NOW)


def test_expired_worker_and_running_job_are_recovered(tmp_path: Path) -> None:
    jobs = queue(tmp_path)
    jobs.enqueue("visual-pending", job_id="recover-me", max_attempts=3, now=NOW)
    assert jobs.acquire_worker("worker-a", ttl_seconds=10, now=NOW)
    first = jobs.claim_next("worker-a", ttl_seconds=10, now=NOW)
    assert first is not None and first.attempt_count == 1

    later = NOW + timedelta(seconds=11)
    assert jobs.acquire_worker("worker-b", ttl_seconds=10, now=later)
    assert jobs.recover_stale_jobs(now=later) == 1
    recovered = jobs.get("recover-me")
    assert recovered.status == "queued"
    assert recovered.error_message == "stale worker lease recovered"

    second = jobs.claim_next("worker-b", ttl_seconds=10, now=later)
    assert second is not None
    assert second.attempt_count == 2
    assert second.lease_owner == "worker-b"


def test_retry_limit_requeues_then_fails(tmp_path: Path) -> None:
    jobs = queue(tmp_path)
    jobs.enqueue("visual-embeddings", job_id="retry-me", max_attempts=2, now=NOW)
    assert jobs.acquire_worker("worker-a", ttl_seconds=30, now=NOW)

    first = jobs.claim_next("worker-a", ttl_seconds=30, now=NOW)
    assert first is not None
    assert jobs.fail(first.job_id, "worker-a", "first failure", retryable=True) == "queued"

    second = jobs.claim_next("worker-a", ttl_seconds=30, now=NOW)
    assert second is not None and second.attempt_count == 2
    assert jobs.fail(second.job_id, "worker-a", "second failure", retryable=True) == "failed"
    failed = jobs.get("retry-me")
    assert failed.status == "failed"
    assert failed.error_message == "second failure"

    retried = jobs.retry("retry-me")
    assert retried.status == "queued"
    assert retried.attempt_count == 0


def test_claim_requires_current_singleton_lease(tmp_path: Path) -> None:
    jobs = queue(tmp_path)
    jobs.enqueue("visual-pending", job_id="lease-required", now=NOW)
    with pytest.raises(IndexJobQueueError, match="worker lease"):
        jobs.claim_next("not-owner", ttl_seconds=30, now=NOW)


def test_payload_schema_rejects_arbitrary_execution_fields(tmp_path: Path) -> None:
    jobs = queue(tmp_path)
    for field in ("command", "script", "path", "model", "url"):
        with pytest.raises(ValueError, match="unsupported payload fields"):
            jobs.enqueue("visual-pending", payload={field: "anything"})


def test_snapshot_reports_queue_and_worker_state(tmp_path: Path) -> None:
    jobs = queue(tmp_path)
    jobs.enqueue("sync-stream-media", job_id="queued-job", now=NOW)
    assert jobs.acquire_worker("worker-a", ttl_seconds=30, now=NOW)
    snapshot = jobs.snapshot()
    assert snapshot["counts"] == {"queued": 1}
    assert snapshot["worker"] is not None
    assert snapshot["worker"]["owner_id"] == "worker-a"
