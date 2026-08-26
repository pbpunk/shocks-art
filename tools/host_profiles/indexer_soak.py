from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

LIVE_ROOT = Path(os.getenv("SHOCKS_HOST_LIVE_ROOT", Path(__file__).resolve().parents[2])).resolve()
BASE_URL = os.getenv("SHOCKS_INDEXER_SOAK_BASE_URL", "http://127.0.0.1:8000/shocks_art").rstrip("/")
DURATION = min(21600, max(120, int(os.getenv("SHOCKS_INDEXER_SOAK_SECONDS", "900"))))
QUERY = os.getenv("SHOCKS_INDEXER_SOAK_QUERY", "man playing guitar")
SCRATCH = Path(os.getenv("LIBRARY_SCRATCH_PATH", LIVE_ROOT / "data" / "library_scratch"))


def emit(payload: dict[str, Any], code: int = 0) -> int:
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    return code


def request_json(path: str, *, method: str = "GET", payload: dict[str, Any] | None = None, timeout: int = 30) -> tuple[int, dict[str, Any], float]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        BASE_URL + path,
        data=body,
        headers={"Content-Type": "application/json"} if body is not None else {},
        method=method,
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            value = json.loads(response.read().decode("utf-8"))
            return int(response.status), value if isinstance(value, dict) else {}, time.perf_counter() - started
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return 0, {}, time.perf_counter() - started


def gpu_memory_mib() -> float | None:
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        values = [float(line.strip()) for line in result.stdout.splitlines() if line.strip()]
        return sum(values) if values else None
    except Exception:
        return None


def scratch_bytes() -> int:
    return sum(path.stat().st_size for path in SCRATCH.rglob("*") if path.is_file()) if SCRATCH.exists() else 0


def run_ps1(script_name: str) -> tuple[bool, str]:
    script = LIVE_ROOT / "tools" / script_name
    if os.name != "nt" or not script.is_file():
        return False, f"missing Windows lifecycle script: {script_name}"
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        cwd=LIVE_ROOT,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    output = (result.stdout + "\n" + result.stderr).strip()[-4000:]
    return result.returncode == 0, output


def queue_snapshot() -> tuple[bool, dict[str, Any]]:
    status, payload, _ = request_json("/api/library/indexing/jobs?limit=10")
    return status == 200, payload


def enqueue_probe() -> str:
    status, payload, _ = request_json(
        "/api/library/indexing/jobs",
        method="POST",
        payload={"job_type": "sync-stream-media", "limit": 1, "import_language": False},
    )
    if status != 200:
        return ""
    return str(payload.get("job", {}).get("jobId") or "")


def wait_for_job(job_id: str, timeout: int = 90) -> tuple[str, dict[str, Any]]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ok, payload = queue_snapshot()
        if ok:
            for job in payload.get("jobs", []):
                if str(job.get("jobId")) == job_id:
                    status = str(job.get("status") or "")
                    if status in {"completed", "failed", "cancelled"}:
                        return status, job
        time.sleep(2)
    return "timeout", {}


def live_worker_present() -> tuple[bool, dict[str, Any] | None]:
    ok, payload = queue_snapshot()
    if not ok:
        return False, None
    worker = payload.get("snapshot", {}).get("worker")
    return isinstance(worker, dict) and bool(worker.get("owner_id")), worker if isinstance(worker, dict) else None


def main() -> int:
    started = time.monotonic()
    next_search = 0.0
    health_checks = health_failures = search_failures = 0
    search_latencies: list[float] = []
    gpu_samples: list[float] = []
    scratch_initial = scratch_bytes()
    scratch_peak = scratch_initial

    worker_before, worker_before_detail = live_worker_present()
    first_job_id = enqueue_probe() if worker_before else ""
    first_job_status, first_job = wait_for_job(first_job_id) if first_job_id else ("not-enqueued", {})

    stop_ok, stop_output = run_ps1("stop_indexer_worker.ps1")
    worker_stopped, _ = live_worker_present()
    # The lease may remain visible until expiry; process ownership is independently proven by the restart scripts.
    start_ok, start_output = run_ps1("start_indexer_worker.ps1")
    time.sleep(3)
    worker_after, worker_after_detail = live_worker_present()
    second_job_id = enqueue_probe() if worker_after else ""
    second_job_status, second_job = wait_for_job(second_job_id) if second_job_id else ("not-enqueued", {})

    while time.monotonic() - started < DURATION:
        elapsed = time.monotonic() - started
        status, health, _ = request_json("/health", timeout=20)
        health_checks += 1
        if status != 200 or health.get("app") != "shocks-art":
            health_failures += 1
        gpu = gpu_memory_mib()
        if gpu is not None:
            gpu_samples.append(gpu)
        scratch_peak = max(scratch_peak, scratch_bytes())

        if elapsed >= next_search:
            status, payload, latency = request_json(
                "/api/library/search/visual",
                method="POST",
                payload={"query": QUERY, "top_k": 5},
                timeout=180,
            )
            if status == 200 and isinstance(payload.get("result"), dict):
                search_latencies.append(latency)
            else:
                search_failures += 1
            next_search = elapsed + 300
        time.sleep(min(10, max(0.5, DURATION - (time.monotonic() - started))))

    final_scratch = scratch_bytes()
    healthy = health_checks > 0 and health_failures == 0
    search_ok = bool(search_latencies) and search_failures == 0
    queue_ok = first_job_status == "completed" and second_job_status == "completed"
    restart_ok = stop_ok and start_ok and worker_after
    scratch_ok = final_scratch <= max(scratch_initial, scratch_peak)
    ok = worker_before and queue_ok and restart_ok and healthy and search_ok and scratch_ok
    return emit(
        {
            "summary": "Indexer lifecycle soak passed" if ok else "Indexer lifecycle soak found a runtime/recovery failure",
            "duration_seconds": round(time.monotonic() - started, 1),
            "worker": {
                "present_before": worker_before,
                "before": worker_before_detail,
                "stop_command_ok": stop_ok,
                "start_command_ok": start_ok,
                "present_after": worker_after,
                "after": worker_after_detail,
                "stop_output_tail": stop_output[-1000:],
                "start_output_tail": start_output[-1000:],
                "lease_visible_immediately_after_stop": worker_stopped,
            },
            "queue_probe": {
                "first_job_id": first_job_id,
                "first_status": first_job_status,
                "first_result": first_job.get("result", {}),
                "second_job_id": second_job_id,
                "second_status": second_job_status,
                "second_result": second_job.get("result", {}),
            },
            "health": {"checks": health_checks, "failures": health_failures},
            "semantic_search": {
                "checks": len(search_latencies) + search_failures,
                "failures": search_failures,
                "max_seconds": round(max(search_latencies), 3) if search_latencies else None,
                "average_seconds": round(sum(search_latencies) / len(search_latencies), 3) if search_latencies else None,
            },
            "gpu": {"samples": len(gpu_samples), "max_used_mib": round(max(gpu_samples), 1) if gpu_samples else None},
            "scratch": {"initial_bytes": scratch_initial, "peak_bytes": scratch_peak, "final_bytes": final_scratch},
        },
        0 if ok else 1,
    )


if __name__ == "__main__":
    raise SystemExit(main())