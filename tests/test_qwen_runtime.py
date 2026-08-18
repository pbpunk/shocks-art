import json
from pathlib import Path

import pytest

from app.indexing.embeddings import EmbeddingBackendError
from app.indexing.qwen_runtime import (
    inspect_qwen_runtime,
    load_qwen_runtime_config,
    require_qwen_runtime,
)


def write_config(path: Path, *, instruction: str = "Retrieve relevant media.") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "runtimeRoot": "data/qwen_indexing",
                "qwenSource": {
                    "repository": "https://example.invalid/Qwen3-VL-Embedding.git",
                    "commit": "a" * 40,
                },
                "model": {
                    "id": "Qwen/Qwen3-VL-Embedding-2B",
                    "revision": "b" * 40,
                    "directoryName": "Qwen3-VL-Embedding-2B",
                    "dtype": "bfloat16",
                    "nativeDimension": 2048,
                },
                "inference": {
                    "instruction": instruction,
                    "recommendedImageBatchSize": 12,
                    "largestValidatedImageBatchSize": 16,
                },
            }
        ),
        encoding="utf-8",
    )


def test_runtime_config_loads_pinned_identity(tmp_path):
    config_path = tmp_path / "runtime.json"
    write_config(config_path)

    config = load_qwen_runtime_config(config_path)

    assert config.model_id == "Qwen/Qwen3-VL-Embedding-2B"
    assert config.model_revision == "b" * 40
    assert config.qwen_commit == "a" * 40
    assert config.native_dimension == 2048
    assert config.instruction == "Retrieve relevant media."
    assert config.recommended_image_batch_size == 12
    assert config.largest_validated_image_batch_size == 16
    assert config.embedding_generation_id.startswith(
        "Qwen/Qwen3-VL-Embedding-2B@bbbbbbbbbbbb#cfg-"
    )
    assert len(config.embedding_configuration_hash) == 64


def test_embedding_generation_changes_when_semantic_instruction_changes(tmp_path):
    first_path = tmp_path / "first.json"
    second_path = tmp_path / "second.json"
    write_config(first_path, instruction="Retrieve relevant media.")
    write_config(second_path, instruction="Represent the visual subject.")

    first = load_qwen_runtime_config(first_path)
    second = load_qwen_runtime_config(second_path)

    assert first.embedding_configuration_hash != second.embedding_configuration_hash
    assert first.embedding_generation_id != second.embedding_generation_id


def test_missing_runtime_is_reported_without_importing_ml_stack(tmp_path):
    config_path = tmp_path / "runtime.json"
    write_config(config_path)

    status = inspect_qwen_runtime(project_root=tmp_path, config_path=config_path)

    assert status.available is False
    assert len(status.problems) == 5
    assert all(str(tmp_path) not in problem for problem in status.problems)

    with pytest.raises(EmbeddingBackendError, match="tools/setup_qwen_indexing.ps1"):
        require_qwen_runtime(project_root=tmp_path, config_path=config_path)


def test_complete_runtime_is_available_and_paths_are_project_relative(tmp_path):
    config_path = tmp_path / "runtime.json"
    write_config(config_path)
    runtime = tmp_path / "data" / "qwen_indexing"

    python = runtime / ".venv" / "Scripts" / "python.exe"
    qwen_source = runtime / "Qwen3-VL-Embedding" / "src" / "models" / "qwen3_vl_embedding.py"
    qwen_head = runtime / "Qwen3-VL-Embedding" / ".git" / "HEAD"
    model_config = runtime / "models" / "Qwen3-VL-Embedding-2B" / "config.json"
    model_revision = runtime / "models" / "Qwen3-VL-Embedding-2B" / ".shocks-art-model-revision"

    for path, content in (
        (python, "test"),
        (qwen_source, "test"),
        (qwen_head, "a" * 40),
        (model_config, "test"),
        (model_revision, "b" * 40),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    status = require_qwen_runtime(project_root=tmp_path, config_path=config_path)
    payload = status.as_dict(project_root=tmp_path)

    assert status.available is True
    assert status.problems == ()
    assert payload["available"] is True
    assert payload["modelId"] == "Qwen/Qwen3-VL-Embedding-2B"
    assert payload["embeddingGenerationId"] == status.config.embedding_generation_id
    assert payload["embeddingConfigurationHash"] == status.config.embedding_configuration_hash
    assert payload["paths"]["python"] == "data/qwen_indexing/.venv/Scripts/python.exe"
    assert payload["paths"]["qwenRepo"] == "data/qwen_indexing/Qwen3-VL-Embedding"
    assert payload["paths"]["modelDir"] == "data/qwen_indexing/models/Qwen3-VL-Embedding-2B"
    assert payload["paths"]["modelRevisionMarker"].endswith("/.shocks-art-model-revision")


def test_revision_mismatch_blocks_runtime(tmp_path):
    config_path = tmp_path / "runtime.json"
    write_config(config_path)
    runtime = tmp_path / "data" / "qwen_indexing"
    required_files = {
        runtime / ".venv" / "Scripts" / "python.exe": "test",
        runtime / "Qwen3-VL-Embedding" / "src" / "models" / "qwen3_vl_embedding.py": "test",
        runtime / "Qwen3-VL-Embedding" / ".git" / "HEAD": "c" * 40,
        runtime / "models" / "Qwen3-VL-Embedding-2B" / "config.json": "test",
        runtime / "models" / "Qwen3-VL-Embedding-2B" / ".shocks-art-model-revision": "d" * 40,
    }
    for path, content in required_files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    status = inspect_qwen_runtime(project_root=tmp_path, config_path=config_path)

    assert status.available is False
    assert any("source revision mismatch" in problem for problem in status.problems)
    assert any("model revision mismatch" in problem for problem in status.problems)
