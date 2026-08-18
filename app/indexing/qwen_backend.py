from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Sequence

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
        return self._invoke({"operation": "text", "values": values}, expected_count=len(values))

    def embed_images(self, image_paths: Sequence[Path]) -> list[Vector]:
        values = [str(Path(path).resolve()) for path in image_paths]
        if not values:
            return []
        return self._invoke({"operation": "images", "values": values}, expected_count=len(values))

    def embed_text_and_images(
        self,
        texts: Sequence[str],
        image_paths: Sequence[Path],
    ) -> tuple[list[Vector], list[Vector]]:
        """Encode text and image inputs in one isolated model load.

        This is intentionally an optimization outside the minimal EmbeddingBackend
        protocol. Localized refinement needs one query plus a small batch of dense
        frames; loading Qwen separately for text and images would dominate runtime.
        """

        text_values = [str(value) for value in texts]
        image_values = [str(Path(path).resolve()) for path in image_paths]
        if not text_values and not image_values:
            return [], []
        items: list[dict[str, str]] = [
            {"kind": "text", "value": value} for value in text_values
        ] + [
            {"kind": "image", "value": value} for value in image_values
        ]
        vectors = self._invoke(
            {"operation": "mixed", "items": items},
            expected_count=len(items),
        )
        text_count = len(text_values)
        return vectors[:text_count], vectors[text_count:]

    def _invoke(self, payload: dict[str, Any], *, expected_count: int) -> list[Vector]:
        paths = self.runtime_status.paths
        config = self.runtime_status.config
        worker = self.project_root / "tools" / "qwen_embedding_worker.py"
        if not worker.is_file():
            raise EmbeddingBackendError(f"Qwen embedding worker is missing: {worker}")

        paths.runtime_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="embedding-request-", dir=paths.runtime_root) as temp_dir:
            request_path = Path(temp_dir) / "request.json"
            response_path = Path(temp_dir) / "response.json"
            request_path.write_text(json.dumps(payload), encoding="utf-8")
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
                response = json.loads(response_path.read_text(encoding="utf-8"))
                vectors = response["vectors"]
            except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
                raise EmbeddingBackendError(f"Qwen embedding worker returned an invalid response: {exc}") from exc

        if not isinstance(vectors, list) or len(vectors) != expected_count:
            raise EmbeddingBackendError(
                f"Qwen embedding worker returned {len(vectors) if isinstance(vectors, list) else 'invalid'} vectors for {expected_count} inputs."
            )
        materialized = [[float(value) for value in vector] for vector in vectors]
        for index, vector in enumerate(materialized):
            if len(vector) != self.dimension:
                raise EmbeddingBackendError(
                    f"Qwen vector {index} has dimension {len(vector)}; expected {self.dimension}."
                )
        return materialized
