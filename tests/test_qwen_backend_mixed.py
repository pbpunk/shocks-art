import json
import subprocess
from pathlib import Path

from app.indexing.qwen_backend import QwenSubprocessEmbeddingBackend
from app.indexing.qwen_runtime import QwenRuntimeConfig, QwenRuntimePaths, QwenRuntimeStatus


def runtime_status(tmp_path: Path) -> QwenRuntimeStatus:
    runtime_root = tmp_path / "data" / "qwen_indexing"
    model_dir = runtime_root / "models" / "Qwen3-VL-Embedding-2B"
    paths = QwenRuntimePaths(
        runtime_root=runtime_root,
        python=runtime_root / ".venv" / "Scripts" / "python.exe",
        qwen_repo=runtime_root / "Qwen3-VL-Embedding",
        model_dir=model_dir,
        package_freeze=runtime_root / "environment.freeze.txt",
        model_revision_marker=model_dir / ".shocks-art-model-revision",
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


def test_mixed_embedding_uses_one_worker_request_and_preserves_order(tmp_path, monkeypatch):
    worker = tmp_path / "tools" / "qwen_embedding_worker.py"
    worker.parent.mkdir(parents=True)
    worker.write_text("# fake worker", encoding="utf-8")
    status = runtime_status(tmp_path)
    status.paths.runtime_root.mkdir(parents=True, exist_ok=True)
    captured = []

    def fake_run(command, **kwargs):
        request_path = Path(command[command.index("--request") + 1])
        response_path = Path(command[command.index("--response") + 1])
        request = json.loads(request_path.read_text(encoding="utf-8"))
        captured.append(request)
        vectors = []
        for index, item in enumerate(request["items"]):
            assert item["kind"] in {"text", "image"}
            vectors.append([float(index + 1), 0.0, 0.0, 0.0])
        response_path.write_text(json.dumps({"ok": True, "vectors": vectors}), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("app.indexing.qwen_backend.subprocess.run", fake_run)
    backend = QwenSubprocessEmbeddingBackend(project_root=tmp_path, runtime_status=status)

    text_vectors, image_vectors = backend.embed_text_and_images(
        ["guitar"],
        [tmp_path / "a.jpg", tmp_path / "b.jpg"],
    )

    assert len(captured) == 1
    assert captured[0]["operation"] == "mixed"
    assert [item["kind"] for item in captured[0]["items"]] == ["text", "image", "image"]
    assert text_vectors == [[1.0, 0.0, 0.0, 0.0]]
    assert image_vectors == [
        [2.0, 0.0, 0.0, 0.0],
        [3.0, 0.0, 0.0, 0.0],
    ]
