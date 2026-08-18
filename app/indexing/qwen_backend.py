from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Sequence

from app.indexing.embeddings import EmbeddingBackendError, Vector
from app.indexing.qwen_runtime import PROJECT_ROOT, QwenRuntimeStatus, require_qwen_runtime


class QwenSubprocessEmbeddingBackend:
    """EmbeddingBackend that keeps Torch/Qwen inside the isolated indexing venv."""

    def __init__(
        self,
        *,
        project_root: Path | None = None,
        runtime_status: QwenRuntimeStatus | None = None,
        timeout_seconds: int = 1800,
    ) -> None:
        self.project_root = Path(project_root or PROJECT_ROOT).resolve()
        self.runtime_status = runtime_status or require_qwen_runtime(project_root=self.project_root)
        self.timeout_seconds = timeout_seconds
        self.model_id = self.runtime_status.config.embedding_generation_id
        self.dimension = self.runtime_status.config.native_dimension

    def embed_text(self, texts: Sequence[str]) -> list[Vector]:
        values = [str(value) for value in texts]
        if not values:
            return []
        return self._invoke("text", values)

    def embed_images(self, image_paths: Sequence[Path]) -> list[Vector]:
        values = [str(Path(path).resolve()) for path in image_paths]
        if not values:
            return []
        return self._invoke("images", values)

    def _invoke(self, operation: str, values: list[str]) -> list[Vector]:
        paths = self.runtime_status.paths
        config = self.runtime_status.config
        worker = self.project_root / "tools" / "qwen_embedding_worker.py"
        if not worker.is_file():
            raise EmbeddingBackendError(f"Qwen embedding worker is missing: {worker}")

        paths.runtime_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="embedding-request-", dir=paths.runtime_root) as temp_dir:
            request_path = Path(temp_dir) / "request.json"
            response_path = Path(temp_dir) / "response.json"
            request_path.write_text(
                json.dumps({"operation": operation, "values": values}),
                encoding="utf-8",
            )
            command = [
                str(paths.python),
                str(worker),
                "--qwen-repo",
                str(paths.qwen_repo),
                "--model-dir",
                str(paths.model_dir),
                "--request",
                str(request_path),
                "--response",
                str(response_path),
                "--dtype",
                config.dtype,
                "--instruction",
                config.instruction,
                "--batch-size",
                str(config.recommended_image_batch_size),
            ]
            try:
                completed = subprocess.run(
                    command,
                    cwd=self.project_root,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise EmbeddingBackendError(f"Qwen embedding worker could not run: {exc}") from exc

            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "no worker output").strip()[-3000:]
                raise EmbeddingBackendError(
                    f"Qwen embedding worker failed with exit code {completed.returncode}: {detail}"
                )
            if not response_path.is_file():
                raise EmbeddingBackendError("Qwen embedding worker completed without a response file.")

            try:
                payload = json.loads(response_path.read_text(encoding="utf-8"))
                vectors = payload["vectors"]
            except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
                raise EmbeddingBackendError(f"Qwen embedding worker returned an invalid response: {exc}") from exc

        if not isinstance(vectors, list) or len(vectors) != len(values):
            raise EmbeddingBackendError(
                f"Qwen embedding worker returned {len(vectors) if isinstance(vectors, list) else 'invalid'} vectors for {len(values)} inputs."
            )
        materialized = [[float(value) for value in vector] for vector in vectors]
        for index, vector in enumerate(materialized):
            if len(vector) != self.dimension:
                raise EmbeddingBackendError(
                    f"Qwen vector {index} has dimension {len(vector)}; expected {self.dimension}."
                )
        return materialized
