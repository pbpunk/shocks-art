from __future__ import annotations

import json
import sys
from pathlib import Path

from tools import qwen_query_worker


def test_qwen_query_worker_preserves_startup_failure_status(tmp_path, monkeypatch):
    runtime_dir = tmp_path / "query-worker"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "qwen_query_worker.py",
            "--qwen-repo", str(tmp_path / "missing-repo"),
            "--model-dir", str(tmp_path / "missing-model"),
            "--runtime-dir", str(runtime_dir),
            "--dtype", "float16",
            "--instruction", "Represent this query",
            "--batch-size", "2",
            "--generation-id", "generation-test",
            "--dimension", "3",
            "--protocol-version", str(qwen_query_worker.WORKER_PROTOCOL_VERSION),
        ],
    )

    assert qwen_query_worker.main() == 1
    status = json.loads((runtime_dir / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "failed"
    assert status["generationId"] == "generation-test"
    assert status["workerProtocolVersion"] == qwen_query_worker.WORKER_PROTOCOL_VERSION
    assert "Qwen implementation is missing" in status["error"]


def test_qwen_query_worker_retries_transient_request_permission_error(tmp_path, monkeypatch):
    request_path = tmp_path / "request.json"
    request_path.write_text('{"operation":"text"}', encoding="utf-8")
    original_read_text = Path.read_text
    attempts = {"count": 0}

    def flaky_read_text(path, *args, **kwargs):
        if path == request_path and attempts["count"] == 0:
            attempts["count"] += 1
            raise PermissionError(13, "Permission denied", str(path))
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky_read_text)
    monkeypatch.setattr(qwen_query_worker.time, "sleep", lambda _seconds: None)

    assert qwen_query_worker._read_published_json(request_path) == {"operation": "text"}
    assert attempts["count"] == 1
