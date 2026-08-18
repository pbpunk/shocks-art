from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.indexing.embeddings import EmbeddingBackendError


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "qwen-indexing-runtime.json"


@dataclass(frozen=True)
class QwenRuntimeConfig:
    runtime_root: str
    model_id: str
    model_revision: str
    model_directory_name: str
    native_dimension: int
    dtype: str
    qwen_repository: str
    qwen_commit: str
    instruction: str
    recommended_image_batch_size: int
    largest_validated_image_batch_size: int

    @property
    def embedding_configuration_hash(self) -> str:
        """Stable identity for settings that can change semantic vectors.

        Throughput-only settings such as batch size are intentionally excluded.
        The hash changes when the model snapshot, implementation, dtype,
        instruction, or native dimension changes, preventing silent generation
        mixing in the Embedding table.
        """

        payload = {
            "modelId": self.model_id,
            "modelRevision": self.model_revision,
            "qwenCommit": self.qwen_commit,
            "dtype": self.dtype,
            "nativeDimension": self.native_dimension,
            "instruction": self.instruction,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @property
    def embedding_generation_id(self) -> str:
        return f"{self.model_id}@{self.model_revision[:12]}#cfg-{self.embedding_configuration_hash[:12]}"


@dataclass(frozen=True)
class QwenRuntimePaths:
    runtime_root: Path
    python: Path
    qwen_repo: Path
    model_dir: Path
    package_freeze: Path


@dataclass(frozen=True)
class QwenRuntimeStatus:
    available: bool
    problems: tuple[str, ...]
    config: QwenRuntimeConfig
    paths: QwenRuntimePaths

    def as_dict(self, *, project_root: Path | None = None) -> dict[str, Any]:
        root = (project_root or PROJECT_ROOT).resolve()

        def relative(path: Path) -> str:
            try:
                return path.resolve().relative_to(root).as_posix()
            except ValueError:
                return path.name

        return {
            "available": self.available,
            "problems": list(self.problems),
            "modelId": self.config.model_id,
            "modelRevision": self.config.model_revision,
            "qwenCommit": self.config.qwen_commit,
            "nativeDimension": self.config.native_dimension,
            "dtype": self.config.dtype,
            "embeddingGenerationId": self.config.embedding_generation_id,
            "embeddingConfigurationHash": self.config.embedding_configuration_hash,
            "recommendedImageBatchSize": self.config.recommended_image_batch_size,
            "largestValidatedImageBatchSize": self.config.largest_validated_image_batch_size,
            "paths": {
                "runtimeRoot": relative(self.paths.runtime_root),
                "python": relative(self.paths.python),
                "qwenRepo": relative(self.paths.qwen_repo),
                "modelDir": relative(self.paths.model_dir),
                "packageFreeze": relative(self.paths.package_freeze),
            },
        }


def load_qwen_runtime_config(path: Path | None = None) -> QwenRuntimeConfig:
    config_path = Path(path or DEFAULT_CONFIG_PATH)
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise EmbeddingBackendError(
            f"Qwen indexing configuration is missing at {config_path}."
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise EmbeddingBackendError(
            f"Qwen indexing configuration could not be read at {config_path}: {exc}"
        ) from exc

    try:
        config = QwenRuntimeConfig(
            runtime_root=str(payload["runtimeRoot"]),
            model_id=str(payload["model"]["id"]),
            model_revision=str(payload["model"]["revision"]),
            model_directory_name=str(payload["model"]["directoryName"]),
            native_dimension=int(payload["model"]["nativeDimension"]),
            dtype=str(payload["model"]["dtype"]),
            qwen_repository=str(payload["qwenSource"]["repository"]),
            qwen_commit=str(payload["qwenSource"]["commit"]),
            instruction=str(payload["inference"]["instruction"]),
            recommended_image_batch_size=int(payload["inference"]["recommendedImageBatchSize"]),
            largest_validated_image_batch_size=int(payload["inference"]["largestValidatedImageBatchSize"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise EmbeddingBackendError(
            f"Qwen indexing configuration is invalid at {config_path}: {exc}"
        ) from exc

    if not config.runtime_root.strip():
        raise EmbeddingBackendError("Qwen runtimeRoot must not be empty.")
    if not config.model_id.strip() or not config.model_revision.strip():
        raise EmbeddingBackendError("Qwen model identity/revision must not be empty.")
    if not config.qwen_repository.strip() or not config.qwen_commit.strip():
        raise EmbeddingBackendError("Qwen source repository/commit must not be empty.")
    if not config.instruction.strip():
        raise EmbeddingBackendError("Qwen inference instruction must not be empty.")
    if config.native_dimension <= 0:
        raise EmbeddingBackendError("Qwen nativeDimension must be greater than zero.")
    if config.recommended_image_batch_size <= 0:
        raise EmbeddingBackendError("Qwen recommendedImageBatchSize must be greater than zero.")
    if config.largest_validated_image_batch_size < config.recommended_image_batch_size:
        raise EmbeddingBackendError(
            "Qwen largestValidatedImageBatchSize must be at least the recommended batch size."
        )
    return config


def qwen_runtime_paths(
    *,
    project_root: Path | None = None,
    config: QwenRuntimeConfig | None = None,
) -> QwenRuntimePaths:
    root = Path(project_root or PROJECT_ROOT).resolve()
    cfg = config or load_qwen_runtime_config()
    runtime_root = root / Path(cfg.runtime_root)
    return QwenRuntimePaths(
        runtime_root=runtime_root,
        python=runtime_root / ".venv" / "Scripts" / "python.exe",
        qwen_repo=runtime_root / "Qwen3-VL-Embedding",
        model_dir=runtime_root / "models" / cfg.model_directory_name,
        package_freeze=runtime_root / "environment.freeze.txt",
    )


def inspect_qwen_runtime(
    *,
    project_root: Path | None = None,
    config_path: Path | None = None,
) -> QwenRuntimeStatus:
    root = Path(project_root or PROJECT_ROOT).resolve()
    config = load_qwen_runtime_config(config_path)
    paths = qwen_runtime_paths(project_root=root, config=config)

    required = (
        (paths.python, "isolated Python interpreter"),
        (paths.qwen_repo / "src" / "models" / "qwen3_vl_embedding.py", "pinned Qwen source"),
        (paths.model_dir / "config.json", "Qwen model snapshot"),
    )
    problems = tuple(
        f"Missing {label}: {path.relative_to(root).as_posix()}"
        for path, label in required
        if not path.is_file()
    )
    return QwenRuntimeStatus(
        available=not problems,
        problems=problems,
        config=config,
        paths=paths,
    )


def require_qwen_runtime(
    *,
    project_root: Path | None = None,
    config_path: Path | None = None,
) -> QwenRuntimeStatus:
    status = inspect_qwen_runtime(project_root=project_root, config_path=config_path)
    if status.available:
        return status
    detail = "; ".join(status.problems)
    raise EmbeddingBackendError(
        "Qwen indexing runtime is unavailable. "
        f"{detail}. Run tools/setup_qwen_indexing.ps1 to create/repair the isolated runtime."
    )
