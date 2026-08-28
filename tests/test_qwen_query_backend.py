from __future__ import annotations

import json
import time
from types import SimpleNamespace

from app.indexing.qwen_query_backend import QwenPersistentQueryEmbeddingBackend
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


def test_query_backend_ready_status_is_generation_locked(tmp_path):
    backend = QwenPersistentQueryEmbeddingBackend(
        project_root=tmp_path,
        runtime_status=_runtime_status(tmp_path),
    )
    status = {
        "state": "ready",
        "generationId": backend.model_id,
        "dimension": backend.dimension,
        "heartbeatEpoch": time.time(),
    }
    assert backend._status_ready(status)
    assert not backend._status_ready({**status, "generationId": "wrong"})
    assert not backend._status_ready({**status, "dimension": 99})


def test_query_backend_waits_for_same_generation_loader_instead_of_spawning(tmp_path, monkeypatch):
    backend = QwenPersistentQueryEmbeddingBackend(
        project_root=tmp_path,
        runtime_status=_runtime_status(tmp_path),
    )
    backend.runtime_dir.mkdir(parents=True)
    backend.status_path.write_text(
        json.dumps(
            {
                "state": "loading",
                "generationId": backend.model_id,
                "dimension": backend.dimension,
                "heartbeatEpoch": time.time(),
            }
        ),
        encoding="utf-8",
    )
    waited = []
    monkeypatch.setattr(backend, "_wait_ready", lambda deadline: waited.append(deadline))
    monkeypatch.setattr("app.indexing.qwen_query_backend.subprocess.Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not spawn duplicate loader")))

    backend._ensure_worker()

    assert len(waited) == 1


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
                "vectors": [[1.0, 2.0, 3.0]],
            }
        ),
        encoding="utf-8",
    )

    assert backend.embed_text(["sanding axes"]) == [[1.0, 2.0, 3.0]]
    request = json.loads((backend.requests_dir / "fixed.json").read_text(encoding="utf-8"))
    assert request["generationId"] == backend.model_id
    assert request["operation"] == "text"
