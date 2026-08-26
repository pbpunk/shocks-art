from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from app.host_bridge import HOST_PROFILES, validate_profile

ROOT = Path(__file__).resolve().parents[1]
PROFILE_SCRIPTS = {
    "whisper-benchmark": ROOT / "tools" / "host_profiles" / "whisper_benchmark.py",
    "private-youtube-probe": ROOT / "tools" / "host_profiles" / "private_youtube_probe.py",
    "indexer-soak": ROOT / "tools" / "host_profiles" / "indexer_soak.py",
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
    result = subprocess.run(
        [python, str(script)], cwd=ROOT, env=os.environ.copy(), text=True,
        capture_output=True, check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")
    if result.stdout:
        print(result.stdout, end="")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
