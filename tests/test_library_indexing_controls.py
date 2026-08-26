from __future__ import annotations

from pathlib import Path

from app.indexing.job_queue import IndexJobQueue


def install_queue(monkeypatch, tmp_path: Path) -> IndexJobQueue:
    queue = IndexJobQueue(tmp_path / "indexing-controls.sqlite3")
    monkeypatch.setattr("app.indexing.control_routes._queue", lambda: queue)
    return queue


def test_indexing_dashboard_is_library_scoped_and_does_not_run_inference(client, monkeypatch, tmp_path):
    install_queue(monkeypatch, tmp_path)
    page = client.get("/shocks_art/library/indexing")
    assert page.status_code == 200
    assert "Library Indexing" in page.text
    assert "Index pending visuals" in page.text
    assert "offline indexing work" in page.text


def test_enqueue_list_cancel_and_retry_are_queue_only(client, monkeypatch, tmp_path):
    queue = install_queue(monkeypatch, tmp_path)

    created = client.post(
        "/shocks_art/api/library/indexing/jobs",
        json={"job_type": "visual-pending"},
    )
    assert created.status_code == 200
    job_id = created.json()["job"]["jobId"]
    assert queue.get(job_id).status == "queued"

    listed = client.get("/shocks_art/api/library/indexing/jobs")
    assert listed.status_code == 200
    assert listed.json()["jobs"][0]["jobId"] == job_id
    assert listed.json()["jobs"][0]["status"] == "queued"

    cancelled = client.post(f"/shocks_art/api/library/indexing/jobs/{job_id}/cancel")
    assert cancelled.status_code == 200
    assert queue.get(job_id).status == "cancelled"

    retried = client.post(f"/shocks_art/api/library/indexing/jobs/{job_id}/retry")
    assert retried.status_code == 200
    assert queue.get(job_id).status == "queued"


def test_running_job_refuses_force_cancel(client, monkeypatch, tmp_path):
    queue = install_queue(monkeypatch, tmp_path)
    job = queue.enqueue("visual-pending")
    assert queue.acquire_worker("worker-test", ttl_seconds=30)
    claimed = queue.claim_next("worker-test", ttl_seconds=30)
    assert claimed is not None and claimed.job_id == job.job_id

    response = client.post(f"/shocks_art/api/library/indexing/jobs/{job.job_id}/cancel")
    assert response.status_code == 409
    assert "cannot be force-cancelled" in response.json()["detail"]
    assert queue.get(job.job_id).status == "running"


def test_control_api_rejects_arbitrary_job_types_and_fields(client, monkeypatch, tmp_path):
    install_queue(monkeypatch, tmp_path)
    arbitrary = client.post(
        "/shocks_art/api/library/indexing/jobs",
        json={"job_type": "shell", "media_id": "whoami"},
    )
    assert arbitrary.status_code == 422

    extra = client.post(
        "/shocks_art/api/library/indexing/jobs",
        json={"job_type": "visual-pending", "command": "whoami"},
    )
    assert extra.status_code == 422
