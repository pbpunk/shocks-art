from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any

LIVE_ROOT = Path(os.getenv("SHOCKS_HOST_LIVE_ROOT", Path(__file__).resolve().parents[2])).resolve()
BASE_URL = os.getenv("SHOCKS_INDEXER_SOAK_BASE_URL", "http://127.0.0.1:8000/shocks_art").rstrip("/")
DURATION = min(21600, max(60, int(os.getenv("SHOCKS_INDEXER_SOAK_SECONDS", "900"))))
RESTART = os.getenv("SHOCKS_INDEXER_SOAK_RESTART", "0") == "1"
QUERY = os.getenv("SHOCKS_INDEXER_SOAK_QUERY", "man playing guitar")
SCRATCH = Path(os.getenv("LIBRARY_SCRATCH_PATH", LIVE_ROOT / "data" / "library_scratch"))


def emit(payload: dict[str, Any], code: int = 0) -> int:
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    return code


def get_json(path: str, timeout: int = 20) -> tuple[int, dict[str, Any]]:
    try:
        with urllib.request.urlopen(BASE_URL + path, timeout=timeout) as response:
            return int(response.status), json.loads(response.read().decode("utf-8"))
    except Exception:
        return 0, {}


def post_json(path: str, payload: dict[str, Any], timeout: int = 180) -> tuple[int, dict[str, Any], float]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(BASE_URL + path, data=body, headers={"Content-Type": "application/json"}, method="POST")
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), json.loads(response.read().decode("utf-8")), time.perf_counter() - started
    except Exception:
        return 0, {}, time.perf_counter() - started


def gpu_memory_mib() -> float | None:
    try:
        result = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"], text=True, capture_output=True, timeout=10, check=False)
        values = [float(line.strip()) for line in result.stdout.splitlines() if line.strip()]
        return sum(values) if values else None
    except Exception:
        return None


def scratch_bytes() -> int:
    return sum(path.stat().st_size for path in SCRATCH.rglob("*") if path.is_file()) if SCRATCH.exists() else 0


def restart_app() -> bool:
    command = LIVE_ROOT / "Restart App.cmd"
    if not command.is_file() or os.name != "nt":
        return False
    result = subprocess.run(["cmd.exe", "/c", str(command)], cwd=LIVE_ROOT, timeout=180, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return result.returncode == 0


def main() -> int:
    started = time.monotonic()
    next_search = 0.0
    restarted = False
    restart_ok: bool | None = None
    health_checks = health_failures = search_failures = 0
    search_latencies: list[float] = []
    gpu_samples: list[float] = []
    scratch_initial = scratch_bytes(); scratch_peak = scratch_initial

    while time.monotonic() - started < DURATION:
        elapsed = time.monotonic() - started
        status, health = get_json("/health")
        health_checks += 1
        if status != 200 or health.get("app") != "shocks-art":
            health_failures += 1
        gpu = gpu_memory_mib()
        if gpu is not None:
            gpu_samples.append(gpu)
        scratch_peak = max(scratch_peak, scratch_bytes())

        if elapsed >= next_search:
            status, payload, latency = post_json("/api/library/search/visual", {"query": QUERY, "top_k": 5})
            if status == 200 and isinstance(payload.get("result"), dict):
                search_latencies.append(latency)
            else:
                search_failures += 1
            next_search = elapsed + 300

        if RESTART and not restarted and elapsed >= DURATION / 2:
            restarted = True
            restart_ok = restart_app()
            # Give the app contract time to return before the next health check.
            time.sleep(5)
        time.sleep(min(10, max(0.5, DURATION - (time.monotonic() - started))))

    final_scratch = scratch_bytes()
    healthy = health_checks > 0 and health_failures == 0
    search_ok = bool(search_latencies) and search_failures == 0
    restart_pass = not RESTART or restart_ok is True
    scratch_ok = final_scratch <= max(scratch_initial, scratch_peak)
    ok = healthy and search_ok and restart_pass and scratch_ok
    return emit({
        "summary": "Indexer live soak passed" if ok else "Indexer live soak found a runtime/recovery failure",
        "duration_seconds": round(time.monotonic() - started, 1),
        "health": {"checks": health_checks, "failures": health_failures},
        "semantic_search": {"checks": len(search_latencies) + search_failures, "failures": search_failures, "max_seconds": round(max(search_latencies), 3) if search_latencies else None, "average_seconds": round(sum(search_latencies) / len(search_latencies), 3) if search_latencies else None},
        "gpu": {"samples": len(gpu_samples), "max_used_mib": round(max(gpu_samples), 1) if gpu_samples else None},
        "scratch": {"initial_bytes": scratch_initial, "peak_bytes": scratch_peak, "final_bytes": final_scratch},
        "restart": {"requested": RESTART, "attempted": restarted, "ok": restart_ok},
    }, 0 if ok else 1)


if __name__ == "__main__":
    raise SystemExit(main())
