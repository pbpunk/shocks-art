from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from tools.host_profiles import indexer_soak as soak


def stage(name: str, **detail: Any) -> None:
    payload = {"stage": name, **detail}
    print("INDEXER_SOAK_STAGE " + json.dumps(payload, separators=(",", ":"), sort_keys=True), file=sys.stderr, flush=True)


def _read_text(stream) -> str:
    stream.flush()
    stream.seek(0)
    return stream.read().decode("utf-8", errors="replace")


def run_ps1_file_backed(script_name: str) -> tuple[bool, str]:
    script = soak.LIVE_ROOT / "tools" / script_name
    stage("lifecycle-start", script=script_name)
    if os.name != "nt" or not script.is_file():
        message = f"missing Windows lifecycle script: {script_name}"
        stage("lifecycle-missing", script=script_name)
        return False, message

    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        try:
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                cwd=soak.LIVE_ROOT,
                stdout=stdout_file,
                stderr=stderr_file,
                timeout=120,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            output = (_read_text(stdout_file) + "\n" + _read_text(stderr_file)).strip()[-4000:]
            stage("lifecycle-timeout", script=script_name)
            return False, output or f"{script_name} timed out"

        output = (_read_text(stdout_file) + "\n" + _read_text(stderr_file)).strip()[-4000:]
    stage("lifecycle-finish", script=script_name, returncode=result.returncode)
    return result.returncode == 0, output


def read_live_metrics_file_backed(model_id: str, dimension: int) -> dict[str, Any]:
    stage("live-metrics-start", model_id=bool(model_id), dimension=dimension)
    if not model_id or dimension <= 0:
        stage("live-metrics-unavailable")
        return {"ok": False, "error_type": "ActiveVisualGenerationUnavailable"}

    env = os.environ.copy()
    env["SHOCKS_HOST_LIVE_ROOT"] = str(soak.LIVE_ROOT)
    env["SHOCKS_INDEXER_SOAK_MODEL_ID"] = model_id
    env["SHOCKS_INDEXER_SOAK_DIMENSION"] = str(dimension)
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        try:
            result = subprocess.run(
                [sys.executable, str(soak.LIVE_METRICS_HELPER)],
                cwd=soak.CODE_ROOT,
                env=env,
                stdout=stdout_file,
                stderr=stderr_file,
                timeout=120,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            stage("live-metrics-timeout")
            return {"ok": False, "error_type": "LiveMetricsTimeout"}

        stdout_text = _read_text(stdout_file)
        stderr_text = _read_text(stderr_file)

    if not stdout_text:
        stage("live-metrics-empty", returncode=result.returncode)
        return {"ok": False, "error_type": f"LiveMetricsExit{result.returncode}"}
    try:
        parsed = json.loads(stdout_text.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        stage("live-metrics-invalid-json", stderr=bool(stderr_text))
        return {"ok": False, "error_type": "LiveMetricsInvalidJson"}
    if not isinstance(parsed, dict):
        stage("live-metrics-invalid-payload")
        return {"ok": False, "error_type": "LiveMetricsInvalidPayload"}
    stage("live-metrics-finish", returncode=result.returncode, ok=bool(parsed.get("ok")))
    return parsed


def wait_for_job_guarded(job_id: str, timeout: int = 90):
    stage("queue-wait-start", job=bool(job_id), timeout=timeout)
    result = _ORIGINAL_WAIT_FOR_JOB(job_id, timeout)
    stage("queue-wait-finish", status=result[0])
    return result


def request_json_guarded(path: str, **kwargs):
    if path == "/api/library/search/visual":
        stage("semantic-search-start")
        result = _ORIGINAL_REQUEST_JSON(path, **kwargs)
        stage("semantic-search-finish", status=result[0], seconds=round(float(result[2]), 3))
        return result
    return _ORIGINAL_REQUEST_JSON(path, **kwargs)


_ORIGINAL_WAIT_FOR_JOB = soak.wait_for_job_with_health
_ORIGINAL_REQUEST_JSON = soak.request_json


def main() -> int:
    stage("profile-start", requested_seconds=soak.REQUESTED_DURATION, effective_seconds=soak.DURATION)
    soak.run_ps1 = run_ps1_file_backed
    soak.read_live_metrics = read_live_metrics_file_backed
    soak.wait_for_job_with_health = wait_for_job_guarded
    soak.request_json = request_json_guarded
    code = soak.main()
    stage("profile-finish", exit_code=code)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
