from __future__ import annotations

import argparse
import json
import math
import platform
import sys
import time
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark Qwen3-VL-Embedding against Shock's Art visual artifacts")
    parser.add_argument("--qwen-repo", required=True, type=Path)
    parser.add_argument("--model-dir", required=True, type=Path)
    parser.add_argument("--artifact-root", type=Path, default=Path("data/library_index/visual"))
    parser.add_argument("--output", type=Path, default=Path("data/qwen_indexing/benchmark.json"))
    parser.add_argument("--batch-sizes", default="1,2,4,8,12,16")
    parser.add_argument("--dimensions", default="2048,1024,512,256")
    parser.add_argument("--query", default="man playing guitar")
    return parser.parse_args()


def mib(value: int | float) -> float:
    return round(float(value) / (1024 * 1024), 2)


def synchronize(torch: Any) -> None:
    if torch.cuda.is_available():
        torch.cuda.synchronize()


def reduced_vector_stats(vector: Any, dimensions: list[int]) -> list[dict[str, Any]]:
    import torch.nn.functional as F

    width = int(vector.shape[-1])
    rows: list[dict[str, Any]] = []
    for dimension in dimensions:
        if dimension <= 0 or dimension > width:
            continue
        reduced = F.normalize(vector[..., :dimension].float(), p=2, dim=-1)
        rows.append(
            {
                "dimension": dimension,
                "float32BytesPerVector": dimension * 4,
                "float16BytesPerVector": dimension * 2,
                "norm": round(float(reduced[0].norm().item()), 6),
            }
        )
    return rows


def main() -> int:
    args = parse_args()
    repo = args.qwen_repo.resolve()
    model_dir = args.model_dir.resolve()
    artifact_root = args.artifact_root.resolve()
    output = args.output.resolve()

    if not (repo / "src" / "models" / "qwen3_vl_embedding.py").is_file():
        raise SystemExit(f"Qwen implementation not found: {repo}")
    if not (model_dir / "config.json").is_file():
        raise SystemExit(f"Qwen model not found: {model_dir}")

    images = sorted(path for path in artifact_root.rglob("*.jpg") if path.is_file())
    if not images:
        raise SystemExit(f"No visual artifacts found under {artifact_root}")

    batch_sizes = sorted({int(value) for value in args.batch_sizes.split(",") if int(value) > 0})
    dimensions = sorted({int(value) for value in args.dimensions.split(",") if int(value) > 0}, reverse=True)

    sys.path.insert(0, str(repo))
    import torch
    from src.models.qwen3_vl_embedding import Qwen3VLEmbedder

    if not torch.cuda.is_available():
        raise SystemExit("CUDA is not available in the isolated Qwen runtime")

    device_index = 0
    torch.cuda.set_device(device_index)
    torch.cuda.empty_cache()
    torch.cuda.reset_peak_memory_stats(device_index)

    load_started = time.perf_counter()
    model = Qwen3VLEmbedder(
        model_name_or_path=str(model_dir),
        torch_dtype=torch.bfloat16,
    )
    synchronize(torch)
    load_seconds = time.perf_counter() - load_started

    loaded_allocated = torch.cuda.memory_allocated(device_index)
    loaded_reserved = torch.cuda.memory_reserved(device_index)
    loaded_peak = torch.cuda.max_memory_allocated(device_index)

    instruction = "Retrieve images or text relevant to the user's query."
    text_input = [{"text": args.query, "instruction": instruction}]

    torch.cuda.reset_peak_memory_stats(device_index)
    text_started = time.perf_counter()
    text_vector = model.process(text_input)
    synchronize(torch)
    text_seconds = time.perf_counter() - text_started
    text_peak = torch.cuda.max_memory_allocated(device_index)

    # Repeat once to prove the loaded runtime remains usable after the first call.
    repeat_started = time.perf_counter()
    repeated_text_vector = model.process(text_input)
    synchronize(torch)
    repeat_seconds = time.perf_counter() - repeat_started
    repeat_cosine = float(torch.nn.functional.cosine_similarity(text_vector.float(), repeated_text_vector.float()).item())

    image_results: list[dict[str, Any]] = []
    best_batch = 0
    first_image_vector = None
    for batch_size in batch_sizes:
        if batch_size > len(images):
            continue
        selected = images[:batch_size]
        torch.cuda.empty_cache()
        torch.cuda.reset_peak_memory_stats(device_index)
        baseline = torch.cuda.memory_allocated(device_index)
        started = time.perf_counter()
        try:
            vectors = model.process(
                [{"image": str(path), "instruction": instruction} for path in selected]
            )
            synchronize(torch)
            elapsed = time.perf_counter() - started
            peak = torch.cuda.max_memory_allocated(device_index)
            result = {
                "batchSize": batch_size,
                "status": "ok",
                "elapsedSeconds": round(elapsed, 4),
                "imagesPerSecond": round(batch_size / elapsed, 4) if elapsed > 0 else None,
                "vectorShape": list(vectors.shape),
                "baselineAllocatedMiB": mib(baseline),
                "peakAllocatedMiB": mib(peak),
                "peakIncrementMiB": mib(max(0, peak - baseline)),
                "reservedMiB": mib(torch.cuda.memory_reserved(device_index)),
            }
            image_results.append(result)
            best_batch = batch_size
            if first_image_vector is None:
                first_image_vector = vectors[:1]
            del vectors
        except torch.cuda.OutOfMemoryError as exc:
            elapsed = time.perf_counter() - started
            image_results.append(
                {
                    "batchSize": batch_size,
                    "status": "oom",
                    "elapsedSeconds": round(elapsed, 4),
                    "error": str(exc)[:1000],
                    "peakAllocatedMiB": mib(torch.cuda.max_memory_allocated(device_index)),
                    "reservedMiB": mib(torch.cuda.memory_reserved(device_index)),
                }
            )
            torch.cuda.empty_cache()
            break

    native_dimension = int(text_vector.shape[-1])
    dimension_stats = reduced_vector_stats(text_vector, dimensions)
    if first_image_vector is not None:
        image_dimension_stats = reduced_vector_stats(first_image_vector, dimensions)
    else:
        image_dimension_stats = []

    payload = {
        "schemaVersion": 1,
        "model": {
            "id": "Qwen/Qwen3-VL-Embedding-2B",
            "path": str(model_dir),
            "nativeDimension": native_dimension,
            "dtype": str(text_vector.dtype),
        },
        "environment": {
            "python": platform.python_version(),
            "torch": torch.__version__,
            "torchCuda": torch.version.cuda,
            "gpu": torch.cuda.get_device_name(device_index),
            "deviceCapability": list(torch.cuda.get_device_capability(device_index)),
            "totalVramMiB": mib(torch.cuda.get_device_properties(device_index).total_memory),
        },
        "modelLoad": {
            "seconds": round(load_seconds, 4),
            "allocatedMiB": mib(loaded_allocated),
            "reservedMiB": mib(loaded_reserved),
            "peakAllocatedMiB": mib(loaded_peak),
        },
        "text": {
            "query": args.query,
            "firstSeconds": round(text_seconds, 4),
            "repeatSeconds": round(repeat_seconds, 4),
            "repeatCosine": round(repeat_cosine, 8),
            "vectorShape": list(text_vector.shape),
            "peakAllocatedMiB": mib(text_peak),
        },
        "images": {
            "availableArtifacts": len(images),
            "largestSuccessfulBatch": best_batch,
            "runs": image_results,
        },
        "dimensions": {
            "text": dimension_stats,
            "image": image_dimension_stats,
            "note": "Reduced dimensions are first-N Matryoshka truncations followed by L2 normalization; retrieval-quality comparison is deferred to semantic evaluation.",
        },
    }

    if not math.isfinite(repeat_cosine):
        raise RuntimeError("Repeated text embedding cosine is not finite")

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
