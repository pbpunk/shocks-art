from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Persistent isolated Qwen3-VL query worker")
    parser.add_argument("--qwen-repo", required=True, type=Path)
    parser.add_argument("--model-dir", required=True, type=Path)
    parser.add_argument("--runtime-dir", required=True, type=Path)
    parser.add_argument("--dtype", default="bfloat16")
    parser.add_argument("--instruction", required=True)
    parser.add_argument("--batch-size", type=int, default=12)
    parser.add_argument("--generation-id", required=True)
    parser.add_argument("--dimension", required=True, type=int)
    return parser.parse_args()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    temporary.replace(path)


def _torch_dtype(torch: Any, name: str) -> Any:
    mapping = {
        "bfloat16": torch.bfloat16,
        "float16": torch.float16,
        "float32": torch.float32,
    }
    normalized = name.strip().lower()
    if normalized not in mapping:
        raise ValueError(f"Unsupported dtype: {name}")
    return mapping[normalized]


def _model_input(kind: str, value: str, instruction: str) -> dict[str, str]:
    if kind == "image":
        return {"image": value, "instruction": instruction}
    return {"text": value, "instruction": instruction}


def _items(payload: dict[str, Any]) -> tuple[str, list[tuple[str, str]]]:
    operation = payload.get("operation")
    if operation == "mixed":
        raw = payload.get("items")
        if not isinstance(raw, list) or not raw:
            raise ValueError("mixed request items must be a non-empty array")
        items: list[tuple[str, str]] = []
        for item in raw:
            if not isinstance(item, dict) or item.get("kind") not in {"text", "image"}:
                raise ValueError("mixed request item kind must be text or image")
            value = item.get("value")
            if not isinstance(value, str) or not value:
                raise ValueError("mixed request values must be non-empty strings")
            items.append((str(item["kind"]), value))
        return operation, items
    if operation not in {"text", "images"}:
        raise ValueError("operation must be text, images, or mixed")
    values = payload.get("values")
    if not isinstance(values, list) or not values or not all(isinstance(value, str) and value for value in values):
        raise ValueError("request values must be a non-empty string array")
    kind = "image" if operation == "images" else "text"
    return operation, [(kind, value) for value in values]


def main() -> int:
    args = parse_args()
    if args.batch_size <= 0 or args.dimension <= 0:
        raise ValueError("batch-size and dimension must be positive")

    runtime_dir = args.runtime_dir.resolve()
    requests_dir = runtime_dir / "requests"
    responses_dir = runtime_dir / "responses"
    status_path = runtime_dir / "status.json"
    stop_path = runtime_dir / "stop.requested"
    requests_dir.mkdir(parents=True, exist_ok=True)
    responses_dir.mkdir(parents=True, exist_ok=True)
    stop_path.unlink(missing_ok=True)

    stopping = False
    failed = False

    def _stop(*_args: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, _stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _stop)

    started = time.time()

    def write_status(state: str, *, error: str = "") -> None:
        _atomic_json(
            status_path,
            {
                "schemaVersion": 1,
                "state": state,
                "pid": os.getpid(),
                "generationId": args.generation_id,
                "dimension": args.dimension,
                "startedAtEpoch": started,
                "heartbeatEpoch": time.time(),
                "error": error[:2000],
            },
        )

    write_status("loading")
    try:
        qwen_repo = args.qwen_repo.resolve()
        model_dir = args.model_dir.resolve()
        if not (qwen_repo / "src" / "models" / "qwen3_vl_embedding.py").is_file():
            raise RuntimeError("Qwen implementation is missing")
        if not (model_dir / "config.json").is_file():
            raise RuntimeError("Qwen model snapshot is missing")

        sys.path.insert(0, str(qwen_repo))
        import torch
        from src.models.qwen3_vl_embedding import Qwen3VLEmbedder

        if not torch.cuda.is_available():
            raise RuntimeError("CUDA is unavailable in the isolated Qwen runtime")
        model = Qwen3VLEmbedder(
            model_name_or_path=str(model_dir),
            torch_dtype=_torch_dtype(torch, args.dtype),
        )
        write_status("ready")
        last_heartbeat = 0.0

        while not stopping and not stop_path.exists():
            now = time.time()
            if now - last_heartbeat >= 2.0:
                write_status("ready")
                last_heartbeat = now

            request_paths = sorted(requests_dir.glob("*.json"), key=lambda path: path.name)
            if not request_paths:
                time.sleep(0.05)
                continue

            request_path = request_paths[0]
            request_id = request_path.stem
            response_path = responses_dir / f"{request_id}.json"
            try:
                payload = json.loads(request_path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("request payload must be an object")
                if payload.get("generationId") != args.generation_id:
                    raise ValueError("request generation does not match the loaded model")
                operation, items = _items(payload)
                vectors: list[list[float]] = []
                for offset in range(0, len(items), args.batch_size):
                    batch = items[offset : offset + args.batch_size]
                    model_inputs = [
                        _model_input(kind, value, args.instruction)
                        for kind, value in batch
                    ]
                    embedded = model.process(model_inputs)
                    batch_vectors = embedded.float().cpu().tolist()
                    del embedded
                    vectors.extend(batch_vectors)
                if any(len(vector) != args.dimension for vector in vectors):
                    raise RuntimeError("Qwen returned an unexpected embedding dimension")
                _atomic_json(
                    response_path,
                    {
                        "ok": True,
                        "requestId": request_id,
                        "operation": operation,
                        "generationId": args.generation_id,
                        "dimension": args.dimension,
                        "vectors": vectors,
                    },
                )
            except Exception as exc:
                _atomic_json(
                    response_path,
                    {
                        "ok": False,
                        "requestId": request_id,
                        "generationId": args.generation_id,
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
            finally:
                request_path.unlink(missing_ok=True)
    except Exception as exc:
        failed = True
        write_status("failed", error=f"{type(exc).__name__}: {exc}")
        return 1
    finally:
        stop_path.unlink(missing_ok=True)
        if not failed:
            try:
                write_status("stopped")
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
