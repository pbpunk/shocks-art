from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

CODE_ROOT = Path(__file__).resolve().parents[2]
LIVE_ROOT = Path(os.getenv("SHOCKS_HOST_LIVE_ROOT", CODE_ROOT)).resolve()
LIVE_METRICS_HELPER = CODE_ROOT / "tools" / "host_profiles" / "indexer_soak_live_metrics.py"
BASE_URL = os.getenv("SHOCKS_INDEXER_SOAK_BASE_URL", "http://127.0.0.1:8000/shocks_art").rstrip("/")
MAX_SOAK_SECONDS = 900


def resolve_soak_duration(raw: str | None) -> tuple[int, int]:
    requested = max(120, int(raw or str(MAX_SOAK_SECONDS)))
    return requested, min(MAX_SOAK_SECONDS, requested)


REQUESTED_DURATION, DURATION = resolve_soak_duration(os.getenv("SHOCKS_INDEXER_SOAK_SECONDS"))
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


def semantic_search_measurement(payload: dict[str, Any], request_seconds: float) -> dict[str, float | int] | None:
    result = payload.get("result")
    if not isinstance(result, dict):
        return None
    fields = {
        "query_embedding_ms": payload.get("queryEmbeddingMs"),
        "vector_retrieval_ms": result.get("elapsedMs"),
        "database_ms": result.get("databaseMs"),
        "scoring_ms": result.get("scoringMs"),
        "vector_count": result.get("vectorCount"),
    }
    try:
        measurement: dict[str, float | int] = {
            "request_seconds": round(float(request_seconds), 4),
            "query_embedding_ms": round(float(fields["query_embedding_ms"]), 4),
            "vector_retrieval_ms": round(float(fields["vector_retrieval_ms"]), 4),
            "database_ms": round(float(fields["database_ms"]), 4),
            "scoring_ms": round(float(fields["scoring_ms"]), 4),
            "vector_count": int(fields["vector_count"]),
        }
    except (TypeError, ValueError):
        return None
    if measurement["vector_count"] < 1:
        return None
    return measurement


def read_live_metrics(model_id: str, dimension: int) -> dict[str, Any]:
    if not model_id or dimension <= 0:
        return {"ok": False, "error_type": "ActiveVisualGenerationUnavailable"}
    env = os.environ.copy()
    env["SHOCKS_HOST_LIVE_ROOT"] = str(LIVE_ROOT)
    env["SHOCKS_INDEXER_SOAK_MODEL_ID"] = model_id
    env["SHOCKS_INDEXER_SOAK_DIMENSION"] = str(dimension)
    try:
        result = subprocess.run(
            [sys.executable, str(LIVE_METRICS_HELPER)],
            cwd=CODE_ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error_type": "LiveMetricsTimeout"}

    if not result.stdout:
        return {"ok": False, "error_type": f"LiveMetricsExit{result.returncode}"}
    try:
        parsed = json.loads(result.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return {"ok": False, "error_type": "LiveMetricsInvalidJson"}
    if not isinstance(parsed, dict):
        return {"ok": False, "error_type": "LiveMetricsInvalidPayload"}
    return parsed


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


def scratch_is_clean(initial_bytes: int, final_bytes: int) -> bool:
    return final_bytes <= initial_bytes


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


def wait_for_job_with_health(job_id: str, timeout: int = 90) -> tuple[str, dict[str, Any], dict[str, Any]]:
    deadline = time.monotonic() + timeout
    overlap = {
        "saw_running": False,
        "running_health_checks": 0,
        "running_health_failures": 0,
    }
    while time.monotonic() < deadline:
        ok, payload = queue_snapshot()
        if ok:
            for job in payload.get("jobs", []):
                if str(job.get("jobId")) != job_id:
                    continue
                status = str(job.get("status") or "")
                if status == "running":
                    overlap["saw_running"] = True
                    health_status, health, _ = request_json("/health", timeout=5)
                    overlap["running_health_checks"] += 1
                    if health_status != 200 or health.get("app") != "shocks-art":
                        overlap["running_health_failures"] += 1
                if status in {"completed", "failed", "cancelled"}:
                    return status, job, overlap
                break
        time.sleep(0.25)
    return "timeout", {}, overlap


def empty_overlap() -> dict[str, Any]:
    return {"saw_running": False, "running_health_checks": 0, "running_health_failures": 0}


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
    search_measurements: list[dict[str, float | int]] = []
    active_model_id = ""
    active_dimension = 0
    gpu_samples: list[float] = []
    scratch_initial = scratch_bytes()
    scratch_peak = scratch_initial

    worker_before, worker_before_detail = live_worker_present()
    first_job_id = enqueue_probe() if worker_before else ""
    first_job_status, first_job, first_overlap = (
        wait_for_job_with_health(first_job_id) if first_job_id else ("not-enqueued", {}, empty_overlap())
    )

    stop_ok, stop_output = run_ps1("stop_indexer_worker.ps1")
    worker_stopped, _ = live_worker_present()
    # The lease may remain visible until expiry; process ownership is independently proven by the restart scripts.
    start_ok, start_output = run_ps1("start_indexer_worker.ps1")
    time.sleep(3)
    worker_after, worker_after_detail = live_worker_present()
    second_job_id = enqueue_probe() if worker_after else ""
    second_job_status, second_job, second_overlap = (
        wait_for_job_with_health(second_job_id) if second_job_id else ("not-enqueued", {}, empty_overlap())
    )

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
            measurement = semantic_search_measurement(payload, latency) if status == 200 else None
            result = payload.get("result") if status == 200 else None
            if measurement is not None and isinstance(result, dict):
                search_measurements.append(measurement)
                active_model_id = str(result.get("modelId") or active_model_id)
                try:
                    active_dimension = int(result.get("dimension") or active_dimension)
                except (TypeError, ValueError):
                    active_dimension = 0
            else:
                search_failures += 1
            next_search = elapsed + 300
        time.sleep(min(10, max(0.5, DURATION - (time.monotonic() - started))))

    final_scratch = scratch_bytes()
    live_metrics = read_live_metrics(active_model_id, active_dimension)
    traces = live_metrics.get("trace_volume") if isinstance(live_metrics.get("trace_volume"), dict) else {
        "total": 0,
        "by_type": {},
        "source": "live-production-sqlite",
        "error_type": str(live_metrics.get("error_type") or "LiveMetricsUnavailable"),
    }
    redundancy = live_metrics.get("long_form_visual_redundancy") if isinstance(live_metrics.get("long_form_visual_redundancy"), dict) else {
        "available": False,
        "model_id": active_model_id,
        "dimension": active_dimension,
        "error_type": str(live_metrics.get("error_type") or "LiveMetricsUnavailable"),
        "metadata_used_for_scoring": False,
        "source": "live-production-sqlite-existing-embeddings",
    }
    trace_inventory_ok = int(traces.get("total") or 0) > 0
    redundancy_ok = bool(redundancy.get("available"))

    running_health_checks = int(first_overlap["running_health_checks"]) + int(second_overlap["running_health_checks"])
    running_health_failures = int(first_overlap["running_health_failures"]) + int(second_overlap["running_health_failures"])
    concurrent_web_ok = running_health_checks > 0 and running_health_failures == 0
    health_ok = health_checks > 0 and health_failures == 0
    search_ok = bool(search_measurements) and search_failures == 0
    queue_ok = first_job_status == "completed" and second_job_status == "completed"
    restart_ok = stop_ok and start_ok and worker_after
    scratch_ok = scratch_is_clean(scratch_initial, final_scratch)
    ok = worker_before and queue_ok and restart_ok and health_ok and concurrent_web_ok and search_ok and scratch_ok and trace_inventory_ok and redundancy_ok
    request_latencies = [float(item["request_seconds"]) for item in search_measurements]
    return emit(
        {
            "summary": "Indexer lifecycle soak passed" if ok else "Indexer lifecycle soak found a runtime/recovery failure",
            "duration_seconds": round(time.monotonic() - started, 1),
            "duration_policy": {
                "requested_seconds": REQUESTED_DURATION,
                "effective_seconds": DURATION,
                "max_seconds": MAX_SOAK_SECONDS,
            },
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
                "first_overlap": first_overlap,
                "second_job_id": second_job_id,
                "second_status": second_job_status,
                "second_result": second_job.get("result", {}),
                "second_overlap": second_overlap,
            },
            "health": {
                "checks": health_checks,
                "failures": health_failures,
                "during_running_jobs": {
                    "checks": running_health_checks,
                    "failures": running_health_failures,
                    "passed": concurrent_web_ok,
                },
            },
            "trace_volume": traces,
            "semantic_search": {
                "checks": len(search_measurements) + search_failures,
                "failures": search_failures,
                "max_seconds": round(max(request_latencies), 4) if request_latencies else None,
                "average_seconds": round(sum(request_latencies) / len(request_latencies), 4) if request_latencies else None,
                "measurements": search_measurements,
                "active_model_id": active_model_id,
                "active_dimension": active_dimension,
                "latency_split": "query_embedding_ms is model/runtime work; vector_retrieval_ms is SQLite load plus in-process cosine scoring",
            },
            "long_form_visual_redundancy": redundancy,
            "live_metrics_isolated": True,
            "gpu": {"samples": len(gpu_samples), "max_used_mib": round(max(gpu_samples), 1) if gpu_samples else None},
            "scratch": {
                "initial_bytes": scratch_initial,
                "peak_bytes": scratch_peak,
                "final_bytes": final_scratch,
                "cleaned": scratch_ok,
            },
        },
        0 if ok else 1,
    )


if __name__ == "__main__":
    raise SystemExit(main())
