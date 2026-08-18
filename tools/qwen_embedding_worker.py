from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Isolated Qwen3-VL embedding worker")
    parser.add_argument("--qwen-repo", required=True, type=Path)
    parser.add_argument("--model-dir", required=True, type=Path)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--response", required=True, type=Path)
    parser.add_argument("--dtype", default="bfloat16")
    parser.add_argument("--instruction", required=True)
    parser.add_argument("--batch-size", type=int, default=12)
    return parser.parse_args()


def _torch_dtype(torch: Any, name: str) -> Any:
    normalized = name.strip().lower()
    mapping = {
        "bfloat16": torch.bfloat16,
        "float16": torch.float16,
        "float32": torch.float32,
    }
    if normalized not in mapping:
        raise ValueError(f"Unsupported dtype: {name}")
    return mapping[normalized]


def _load_request(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("request must be a JSON object")
    operation = payload.get("operation")
    values = payload.get("values")
    if operation not in {"images", "text"}:
        raise ValueError("operation must be 'images' or 'text'")
    if not isinstance(values, list) or not values:
        raise ValueError("values must be a non-empty array")
    if not all(isinstance(value, str) and value for value in values):
        raise ValueError("all values must be non-empty strings")
    return payload


def _write_response(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def main() -> int:
    args = parse_args()
    request = _load_request(args.request)
    qwen_repo = args.qwen_repo.resolve()
    model_dir = args.model_dir.resolve()

    if not (qwen_repo / "src" / "models" / "qwen3_vl_embedding.py").is_file():
        raise RuntimeError(f"Qwen implementation not found: {qwen_repo}")
    if not (model_dir / "config.json").is_file():
        raise RuntimeError(f"Qwen model not found: {model_dir}")
    if args.batch_size <= 0:
        raise ValueError("batch-size must be greater than zero")

    sys.path.insert(0, str(qwen_repo))
    import torch
    from src.models.qwen3_vl_embedding import Qwen3VLEmbedder

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is unavailable in the isolated Qwen runtime")

    model = Qwen3VLEmbedder(
        model_name_or_path=str(model_dir),
        torch_dtype=_torch_dtype(torch, args.dtype),
    )

    values: list[str] = request["values"]
    operation = request["operation"]
    vectors: list[list[float]] = []

    for offset in range(0, len(values), args.batch_size):
        batch = values[offset : offset + args.batch_size]
        if operation == "images":
            model_inputs = [{"image": value, "instruction": args.instruction} for value in batch]
        else:
            model_inputs = [{"text": value, "instruction": args.instruction} for value in batch]
        embedded = model.process(model_inputs)
        vectors.extend(embedded.float().cpu().tolist())
        del embedded

    _write_response(
        args.response,
        {
            "ok": True,
            "operation": operation,
            "count": len(vectors),
            "dimension": len(vectors[0]) if vectors else 0,
            "vectors": vectors,
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
