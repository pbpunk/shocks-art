from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


def emit(payload: dict[str, Any], code: int = 0) -> int:
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    return code


def gpu_memory_mib() -> float | None:
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            return float(result.stdout.splitlines()[0].strip())
    except Exception:
        pass
    return None


def main() -> int:
    duration = int(os.getenv("SHOCKS_INDEXER_SOAK_SECONDS", "300"))
    duration = min(3600, max(60, duration))
    scratch = Path(os.getenv("SHOCKS_HOST_SCRATCH_ROOT", str(Path(os.getenv("SHOCKS_HOST_LIVE_ROOT", ROOT)) / "data" / "library_scratch")))
    scratch.mkdir(parents=True, exist_ok=True)
    disk_before = shutil.disk_usage(scratch)
    gpu_before = gpu_memory_mib()
    started = time.monotonic()
    iterations: list[dict[str, Any]] = []
    iteration = 0
    while time.monotonic() - started < duration:
        iteration += 1
        loop_started = time.monotonic()
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "tests/test_indexing.py",
                "tests/test_embedding_indexing.py",
                "tests/test_library_semantic_search.py",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=900,
            check=False,
        )
        iterations.append({
            "iteration": iteration,
            "exit_code": result.returncode,
            "runtime_seconds": round(time.monotonic() - loop_started, 3),
            "output_tail": (result.stdout + "\n" + result.stderr)[-2000:],
            "gpu_memory_mib": gpu_memory_mib(),
        })
        if result.returncode != 0:
            return emit({
                "summary": f"Indexer soak failed on iteration {iteration}",
                "duration_seconds": round(time.monotonic() - started, 3),
                "iterations": iterations,
            }, 1)
        if time.monotonic() - started < duration:
            time.sleep(min(5, max(0, duration - (time.monotonic() - started))))

    disk_after = shutil.disk_usage(scratch)
    return emit({
        "summary": f"Indexer soak completed {iteration} clean validation iterations",
        "duration_seconds": round(time.monotonic() - started, 3),
        "iterations": iterations,
        "disk_free_before": disk_before.free,
        "disk_free_after": disk_after.free,
        "disk_delta_bytes": disk_after.free - disk_before.free,
        "gpu_memory_before_mib": gpu_before,
        "gpu_memory_after_mib": gpu_memory_mib(),
        "scratch_root_exists": scratch.exists(),
    })


if __name__ == "__main__":
    raise SystemExit(main())
