from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

from app.indexing.qwen_query_backend import WORKER_PROTOCOL_VERSION, QwenPersistentQueryEmbeddingBackend
from app.indexing.qwen_runtime import QwenRuntimeConfig, QwenRuntimePaths, QwenRuntimeStatus


def _runtime_status(tmp_path):
    runtime_root = tmp_path / "qwen-runtime"
    config = QwenRuntimeConfig(
        runtime_root="ignored",
        model_id="Qwen/Test",
        model_revision="a" * 40,
        model_directory_name="model",
        native_dimension=3,
        dtype="float16",
        qwen_repository="repo",
        qwen_commit="b" * 40,
        instruction="Represent this query",
        recommended_image_batch_size=2,
        largest_validated_image_batch_size=2,
    )
    paths = QwenRuntimePaths(
        runtime_root=runtime_root,
        python=runtime_root / "python.exe",
        qwen_repo=runtime_root / "repo",
        model_dir=runtime_root / "model",
        package_freeze=runtime_root / "freeze.txt",
        model_revision_marker=runtime_root / "model" / ".revision",
    )
    return QwenRuntimeStatus(available=True, problems=(), config=config, paths=paths)


def _compatible_status(backend, *, state="ready"):
    return {
        "state": state,
        "generationId": backend.model_id,
        "dimension": backend.dimension,
        "workerProtocolVersion": WORKER_PROTOCOL_VERSION,
        "heartbeatEpoch": time.time(),
    }


def test_query_backend_ready_status_is_generation_and_protocol_locked(tmp_path):
    backend = QwenPersistentQueryEmbeddingBackend(
        project_root=tmp_path,
        runtime_status=_runtime_status(tmp_path),
    )
    status = _compatible_status(backend)
    assert backend._status_ready(status)
    assert not backend._status_ready({**status, "generationId": "wrong"})
    assert not backend._status_ready({**status, "dimension": 99})
    assert not backend._status_ready({**status, "workerProtocolVersion": WORKER_PROTOCOL_VERSION + 1})
    assert not backend._status_ready({key: value for key, value in status.items() if key != "workerProtocolVersion"})


def test_query_backend_waits_for_same_generation_loader_instead_of_spawning(tmp_path, monkeypatch):
    backend = QwenPersistentQueryEmbeddingBackend(
        project_root=tmp_path,
        runtime_status=_runtime_status(tmp_path),
    )
    backend.runtime_dir.mkdir(parents=True)
    backend.status_path.write_text(json.dumps(_compatible_status(backend, state="loading")), encoding="utf-8")
    waited = []
    monkeypatch.setattr(backend, "_wait_ready", lambda deadline: waited.append(deadline))
    monkeypatch.setattr("app.indexing.qwen_query_backend.subprocess.Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not spawn duplicate loader")))

    backend._ensure_worker()

    assert len(waited) == 1


def test_query_backend_replaces_same_generation_worker_with_stale_protocol(tmp_path, monkeypatch):
    backend = QwenPersistentQueryEmbeddingBackend(
        project_root=tmp_path,
        runtime_status=_runtime_status(tmp_path),
    )
    worker = tmp_path / "tools" / "qwen_query_worker.py"
    worker.parent.mkdir(parents=True)
    worker.write_text("# test worker\n", encoding="utf-8")
    statuses = iter(
        [
            {**_compatible_status(backend), "workerProtocolVersion": 0},
            {"state": "stopped"},
        ]
    )
    commands = []
    monkeypatch.setattr(backend, "_read_status", lambda: next(statuses))
    monkeypatch.setattr(backend, "_wait_ready", lambda _deadline: None)
    monkeypatch.setattr(
        "app.indexing.qwen_query_backend.subprocess.Popen",
        lambda command, **_kwargs: commands.append(command),
    )

    backend._ensure_worker()

    assert len(commands) == 1
    protocol_index = commands[0].index("--protocol-version")
    assert commands[0][protocol_index + 1] == str(WORKER_PROTOCOL_VERSION)


def test_query_backend_reads_generation_checked_response(tmp_path, monkeypatch):
    backend = QwenPersistentQueryEmbeddingBackend(
        project_root=tmp_path,
        runtime_status=_runtime_status(tmp_path),
    )
    backend.requests_dir.mkdir(parents=True)
    backend.responses_dir.mkdir(parents=True)
    monkeypatch.setattr(backend, "_ensure_worker", lambda: None)
    monkeypatch.setattr("app.indexing.qwen_query_backend.uuid.uuid4", lambda: SimpleNamespace(hex="fixed"))
    (backend.responses_dir / "fixed.json").write_text(
        json.dumps(
            {
                "ok": True,
                "requestId": "fixed",
                "operation": "text",
                "generationId": backend.model_id,
                "dimension": backend.dimension,
                "workerProtocolVersion": WORKER_PROTOCOL_VERSION,
                "vectors": [[1.0, 2.0, 3.0]],
            }
        ),
        encoding="utf-8",
    )

    assert backend.embed_text(["sanding axes"]) == [[1.0, 2.0, 3.0]]
    request = json.loads((backend.requests_dir / "fixed.json").read_text(encoding="utf-8"))
    assert request["generationId"] == backend.model_id
    assert request["workerProtocolVersion"] == WORKER_PROTOCOL_VERSION
    assert request["operation"] == "text"


def test_query_backend_retries_transient_response_permission_error(tmp_path, monkeypatch):
    backend = QwenPersistentQueryEmbeddingBackend(
        project_root=tmp_path,
        runtime_status=_runtime_status(tmp_path),
    )
    response_path = tmp_path / "response.json"
    response_path.write_text('{"ok":true}', encoding="utf-8")
    original_read_text = Path.read_text
    attempts = {"count": 0}

    def flaky_read_text(path, *args, **kwargs):
        if path == response_path and attempts["count"] == 0:
            attempts["count"] += 1
            raise PermissionError(13, "Permission denied", str(path))
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky_read_text)
    monkeypatch.setattr("app.indexing.qwen_query_backend.time.sleep", lambda _seconds: None)

    assert backend._read_published_json(response_path) == {"ok": True}
    assert attempts["count"] == 1
