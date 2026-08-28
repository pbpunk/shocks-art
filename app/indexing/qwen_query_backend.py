from __future__ import annotations

import json
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Sequence

from app.indexing.embeddings import EmbeddingBackendError, Vector
from app.indexing.qwen_runtime import PROJECT_ROOT, QwenRuntimeStatus, require_qwen_runtime


WORKER_PROTOCOL_VERSION = 1
IPC_READ_RETRY_SECONDS = 2.0
IPC_READ_RETRY_INTERVAL_SECONDS = 0.02


class QwenPersistentQueryEmbeddingBackend:
    """Text-query embedding backend backed by one persistent isolated Qwen process.

    Torch/Qwen remain outside the FastAPI/runtime interpreter. The first query may
    pay model startup cost; later queries reuse the same generation-locked worker.
    """

    def __init__(
        self,
        *,
        project_root: Path | None = None,
        runtime_status: QwenRuntimeStatus | None = None,
        startup_timeout_seconds: int = 300,
        request_timeout_seconds: int = 120,
        heartbeat_stale_seconds: float = 15.0,
    ) -> None:
        self.project_root = Path(project_root or PROJECT_ROOT).resolve()
        self.runtime_status = runtime_status or require_qwen_runtime(project_root=self.project_root)
        self.startup_timeout_seconds = startup_timeout_seconds
        self.request_timeout_seconds = request_timeout_seconds
        self.heartbeat_stale_seconds = heartbeat_stale_seconds
        self.model_id = self.runtime_status.config.embedding_generation_id
        self.dimension = self.runtime_status.config.native_dimension
        self.worker_protocol_version = WORKER_PROTOCOL_VERSION
        self.runtime_dir = self.runtime_status.paths.runtime_root / "query-worker"
        self.requests_dir = self.runtime_dir / "requests"
        self.responses_dir = self.runtime_dir / "responses"
        self.status_path = self.runtime_dir / "status.json"
        self.stop_path = self.runtime_dir / "stop.requested"

    def embed_text(self, texts: Sequence[str]) -> list[Vector]:
        values = [str(value) for value in texts]
        if not values:
            return []
        return self._invoke({"operation": "text", "values": values}, expected_count=len(values))

    def _read_status(self) -> dict[str, Any]:
        try:
            value = json.loads(self.status_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _status_compatible(self, status: dict[str, Any]) -> bool:
        try:
            return (
                status.get("generationId") == self.model_id
                and int(status.get("dimension", 0)) == self.dimension
                and int(status.get("workerProtocolVersion", 0)) == self.worker_protocol_version
            )
        except (TypeError, ValueError):
            return False

    def _status_ready(self, status: dict[str, Any]) -> bool:
        try:
            heartbeat = float(status.get("heartbeatEpoch", 0.0))
            return (
                status.get("state") == "ready"
                and self._status_compatible(status)
                and (time.time() - heartbeat) <= self.heartbeat_stale_seconds
            )
        except (TypeError, ValueError):
            return False

    def _wait_ready(self, deadline: float) -> None:
        while time.monotonic() < deadline:
            status = self._read_status()
            if self._status_ready(status):
                return
            if status.get("state") == "failed":
                raise EmbeddingBackendError(
                    f"Persistent Qwen query worker failed: {status.get('error') or 'unknown error'}"
                )
            time.sleep(0.1)
        raise EmbeddingBackendError("Persistent Qwen query worker did not become ready before timeout")

    def _stop_active_worker(self) -> None:
        self.stop_path.write_text("stop\n", encoding="utf-8")
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if self._read_status().get("state") in {"stopped", "failed"}:
                return
            time.sleep(0.1)
        raise EmbeddingBackendError("Persistent Qwen query worker did not stop before replacement")

    def _ensure_worker(self) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.requests_dir.mkdir(parents=True, exist_ok=True)
        self.responses_dir.mkdir(parents=True, exist_ok=True)
        status = self._read_status()
        if self._status_ready(status):
            return

        startup_deadline = time.monotonic() + self.startup_timeout_seconds
        state = status.get("state")
        if state == "loading" and self._status_compatible(status):
            self._wait_ready(startup_deadline)
            return

        # A resident worker is reusable only when both its embedding generation and
        # IPC protocol match this checkout. This makes implementation deployments
        # replace stale same-model workers instead of silently reusing old code.
        if state in {"loading", "ready"}:
            self._stop_active_worker()

        worker = self.project_root / "tools" / "qwen_query_worker.py"
        if not worker.is_file():
            raise EmbeddingBackendError(f"Persistent Qwen query worker is missing: {worker}")
        self.stop_path.unlink(missing_ok=True)
        paths = self.runtime_status.paths
        config = self.runtime_status.config
        command = [
            str(paths.python), str(worker),
            "--qwen-repo", str(paths.qwen_repo),
            "--model-dir", str(paths.model_dir),
            "--runtime-dir", str(self.runtime_dir),
            "--dtype", config.dtype,
            "--instruction", config.instruction,
            "--batch-size", str(config.recommended_image_batch_size),
            "--generation-id", self.model_id,
            "--dimension", str(self.dimension),
            "--protocol-version", str(self.worker_protocol_version),
        ]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            subprocess.Popen(
                command,
                cwd=self.project_root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
                close_fds=True,
            )
        except OSError as exc:
            raise EmbeddingBackendError(f"Persistent Qwen query worker could not start: {exc}") from exc
        self._wait_ready(startup_deadline)

    def _read_published_json(self, path: Path) -> Any:
        deadline = time.monotonic() + IPC_READ_RETRY_SECONDS
        while True:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(IPC_READ_RETRY_INTERVAL_SECONDS)

    def _invoke(self, payload: dict[str, Any], *, expected_count: int) -> list[Vector]:
        self._ensure_worker()
        request_id = uuid.uuid4().hex
        request_path = self.requests_dir / f"{request_id}.json"
        response_path = self.responses_dir / f"{request_id}.json"
        temporary = request_path.with_suffix(".json.tmp")
        request = {
            **payload,
            "generationId": self.model_id,
            "workerProtocolVersion": self.worker_protocol_version,
        }
        temporary.write_text(json.dumps(request, separators=(",", ":")), encoding="utf-8")
        temporary.replace(request_path)

        deadline = time.monotonic() + self.request_timeout_seconds
        while time.monotonic() < deadline:
            if response_path.is_file():
                break
            status = self._read_status()
            if status.get("state") == "failed":
                raise EmbeddingBackendError(
                    f"Persistent Qwen query worker failed during request: {status.get('error') or 'unknown error'}"
                )
            time.sleep(0.02)
        if not response_path.is_file():
            request_path.unlink(missing_ok=True)
            raise EmbeddingBackendError("Persistent Qwen query request timed out")

        try:
            response = self._read_published_json(response_path)
        except (OSError, json.JSONDecodeError) as exc:
            raise EmbeddingBackendError(f"Persistent Qwen query worker returned invalid JSON: {exc}") from exc
        finally:
            try:
                response_path.unlink(missing_ok=True)
            except OSError:
                pass

        if not isinstance(response, dict) or not response.get("ok"):
            raise EmbeddingBackendError(
                f"Persistent Qwen query worker rejected request: {response.get('error') if isinstance(response, dict) else 'invalid response'}"
            )
        if (
            response.get("generationId") != self.model_id
            or int(response.get("dimension", 0)) != self.dimension
            or int(response.get("workerProtocolVersion", 0)) != self.worker_protocol_version
        ):
            raise EmbeddingBackendError("Persistent Qwen query worker returned the wrong embedding generation or protocol")
        vectors = response.get("vectors")
        if not isinstance(vectors, list) or len(vectors) != expected_count:
            raise EmbeddingBackendError(
                f"Persistent Qwen query worker returned {len(vectors) if isinstance(vectors, list) else 'invalid'} vectors for {expected_count} inputs"
            )
        materialized = [[float(value) for value in vector] for vector in vectors]
        if any(len(vector) != self.dimension for vector in materialized):
            raise EmbeddingBackendError("Persistent Qwen query worker returned an unexpected vector dimension")
        return materialized
