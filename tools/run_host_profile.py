from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.host_bridge import HOST_PROFILES, validate_profile

PROFILE_SCRIPTS = {
    "whisper-benchmark": ROOT / "tools" / "host_profiles" / "whisper_benchmark.py",
    "private-youtube-probe": ROOT / "tools" / "host_profiles" / "private_youtube_probe.py",
    "indexer-soak": ROOT / "tools" / "host_profiles" / "indexer_soak.py",
    "repo-tests": ROOT / "tools" / "host_profiles" / "repo_tests.py",
}

# Fixed repository-owned budgets prevent a wedged profile from monopolizing the
# single workstation verifier until the broad worker timeout expires. The Sheet
# cannot alter these values.
PROFILE_TIMEOUT_SECONDS = {
    "repo-tests": 900,
    "private-youtube-probe": 900,
    "indexer-soak": 1800,
    "whisper-benchmark": 7200,
}


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
    try:
        result = subprocess.run(
            [python, str(script)], cwd=ROOT, env=os.environ.copy(), text=True,
            capture_output=True, check=False, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        print(json.dumps({"summary": f"{profile} exceeded its fixed {timeout}-second execution budget", "timed_out": True, "timeout_seconds": timeout}))
        return 124
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")
    if result.stdout:
        print(result.stdout, end="")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
