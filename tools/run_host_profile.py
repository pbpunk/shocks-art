from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.host_bridge import HOST_PROFILES, validate_profile

PROFILE_SCRIPTS = {
    "whisper-benchmark": ROOT / "tools" / "host_profiles" / "whisper_benchmark.py",
    "whisper-benchmark-prepare": ROOT / "tools" / "host_profiles" / "whisper_benchmark_prepare.py",
    "whisper-benchmark-finalize": ROOT / "tools" / "host_profiles" / "whisper_benchmark_finalize.py",
    "private-youtube-probe": ROOT / "tools" / "host_profiles" / "private_youtube_probe.py",
    "indexer-soak": ROOT / "tools" / "host_profiles" / "indexer_soak_guarded.py",
    "clips-native-ask-smoke": ROOT / "tools" / "host_profiles" / "clips_native_ask_smoke.py",
    "clips-native-ask-rerun": ROOT / "tools" / "host_profiles" / "clips_native_ask_rerun.py",
    "derived-data-reinitialize": ROOT / "tools" / "host_profiles" / "derived_data_reinitialize.py",
    "derived-data-resume": ROOT / "tools" / "host_profiles" / "derived_data_resume.py",
    "retrieval-quality": ROOT / "tools" / "host_profiles" / "retrieval_quality.py",
    "cross-modal-overlap-proof": ROOT / "tools" / "host_profiles" / "cross_modal_overlap_proof.py",
    "retrieval-coverage-expand": ROOT / "tools" / "host_profiles" / "retrieval_coverage_expand.py",
    "retrieval-target-diagnostics": ROOT / "tools" / "host_profiles" / "retrieval_target_diagnostics.py",
    "retrieval-depth-sweep": ROOT / "tools" / "host_profiles" / "retrieval_depth_sweep.py",
    "repo-tests": ROOT / "tools" / "host_profiles" / "repo_tests.py",
}

PROFILE_TIMEOUT_SECONDS = {
    "repo-tests": 900,
    "private-youtube-probe": 900,
    "clips-native-ask-smoke": 1200,
    "clips-native-ask-rerun": 1200,
    "retrieval-quality": 2400,
    "retrieval-target-diagnostics": 2400,
    "retrieval-depth-sweep": 2400,
    "cross-modal-overlap-proof": 3600,
    "retrieval-coverage-expand": 7200,
    "indexer-soak": 1800,
    "derived-data-reinitialize": 7200,
    "derived-data-resume": 7200,
    "whisper-benchmark": 7200,
    "whisper-benchmark-prepare": 3600,
    "whisper-benchmark-finalize": 600,
}


def _read_text(stream) -> str:
    stream.flush()
    stream.seek(0)
    return stream.read().decode("utf-8", errors="replace")


def main() -> int:
    if len(sys.argv) != 2:
        print(json.dumps({"summary": f"usage: {Path(sys.argv[0]).name} <{'|'.join(HOST_PROFILES)}>"}))
        return 2
    profile = validate_profile(sys.argv[1])
    script = PROFILE_SCRIPTS[profile]
    python = sys.executable
    if profile == "whisper-benchmark" and os.getenv("SHOCKS_WHISPER_PYTHON", "").strip():
        python = os.environ["SHOCKS_WHISPER_PYTHON"].strip()
    timeout = PROFILE_TIMEOUT_SECONDS[profile]

    # Use real temporary files rather than subprocess.PIPE. Some Windows host profiles
    # deliberately start long-lived detached child processes. If any descendant retains
    # an inherited pipe handle, subprocess.communicate() can wait for EOF even after the
    # profile process itself exits. File-backed capture has no EOF dependency on descendants.
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        try:
            result = subprocess.run(
                [python, str(script)], cwd=ROOT, env=os.environ.copy(),
                stdout=stdout_file, stderr=stderr_file, check=False, timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            stderr_text = _read_text(stderr_file)
            if stderr_text:
                print(stderr_text, file=sys.stderr, end="")
            stdout_text = _read_text(stdout_file)
            if stdout_text:
                print(stdout_text, end="")
            print(json.dumps({"summary": f"{profile} exceeded its fixed {timeout}-second execution budget", "timed_out": True, "timeout_seconds": timeout}))
            return 124

        stderr_text = _read_text(stderr_file)
        stdout_text = _read_text(stdout_file)

    if stderr_text:
        print(stderr_text, file=sys.stderr, end="")
    if stdout_text:
        print(stdout_text, end="")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
