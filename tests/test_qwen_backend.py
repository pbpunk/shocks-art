import json
import subprocess
from pathlib import Path

import pytest

from app.indexing.embeddings import EmbeddingBackendError
from app.indexing.qwen_backend import QwenSubprocessEmbeddingBackend
from app.indexing.qwen_runtime import QwenRuntimeConfig, QwenRuntimePaths, QwenRuntimeStatus


def runtime_status(tmp_path: Path) -> QwenRuntimeStatus:
    runtime_root = tmp_path / "data" / "qwen_indexing"
    paths = QwenRuntimePaths(
        runtime_root=runtime_root,
        python=runtime_root / ".venv" / "Scripts" / "python.exe",
        qwen_repo=runtime_root / "Qwen3-VL-Embedding",
        model_dir=runtime_root / "models" / "Qwen3-VL-Embedding-2B",
        package_freeze=runtime_root / "environment.freeze.txt",
    )
    config = QwenRuntimeConfig(
        runtime_root="data/qwen_indexing",
        model_id="Qwen/Qwen3-VL-Embedding-2B",
        model_revision="b" * 40,
        model_directory_name="Qwen3-VL-Embedding-2B",
        native_dimension=4,
        dtype="bfloat16",
        qwen_repository="https://example.invalid/qwen.git",
        qwen_commit="a" * 40,
        instruction="Retrieve relevant media.",
        recommended_image_batch_size=12,
        largest_validated_image_batch_size=16,
    )
    return QwenRuntimeStatus(available=True, problems=(), config=config, paths=paths)


def test_subprocess_backend_uses_isolated_runtime_and_response_file(tmp_path, monkeypatch):
    worker = tmp_path / "tools" / "qwen_embedding_worker.py"
    worker.parent.mkdir(parents=True)
    worker.write_text("# fake worker", encoding="utf-8")
    status = runtime_status(tmp_path)
    status.paths.runtime_root.mkdir(parents=True, exist_ok=True)
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        response_path = Path(command[command.index("--response") + 1])
        request_path = Path(command[command.index("--request") + 1])
        request = json.loads(request_path.read_text(encoding="utf-8"))
        response_path.write_text(
            json.dumps(
                {
                    "ok": True,
                    "vectors": [[1.0, 2.0, 3.0, 4.0] for _ in request["values"]],
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("app.indexing.qwen_backend.subprocess.run", fake_run)
    backend = QwenSubprocessEmbeddingBackend(project_root=tmp_path, runtime_status=status)

    vectors = backend.embed_text(["guitar", "woodworking"])

    assert backend.dimension == 4
    assert backend.model_id == status.config.embedding_generation_id
    assert vectors == [[1.0, 2.0, 3.0, 4.0], [1.0, 2.0, 3.0, 4.0]]
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command[0] == str(status.paths.python)
    assert command[command.index("--batch-size") + 1] == "12"
    assert command[command.index("--instruction") + 1] == status.config.instruction
    assert kwargs["cwd"] == tmp_path


def test_subprocess_backend_surfaces_worker_failure(tmp_path, monkeypatch):
    worker = tmp_path / "tools" / "qwen_embedding_worker.py"
    worker.parent.mkdir(parents=True)
    worker.write_text("# fake worker", encoding="utf-8")
    status = runtime_status(tmp_path)
    status.paths.runtime_root.mkdir(parents=True, exist_ok=True)

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 7, stdout="", stderr="simulated GPU failure")

    monkeypatch.setattr("app.indexing.qwen_backend.subprocess.run", fake_run)
    backend = QwenSubprocessEmbeddingBackend(project_root=tmp_path, runtime_status=status)

    with pytest.raises(EmbeddingBackendError, match="simulated GPU failure"):
        backend.embed_images([tmp_path / "frame.jpg"])
