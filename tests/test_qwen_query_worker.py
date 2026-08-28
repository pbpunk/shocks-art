from __future__ import annotations

import json
import sys

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
        ],
    )

    assert qwen_query_worker.main() == 1
    status = json.loads((runtime_dir / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "failed"
    assert status["generationId"] == "generation-test"
    assert "Qwen implementation is missing" in status["error"]
