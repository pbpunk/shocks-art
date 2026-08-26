from __future__ import annotations

import json
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
    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")
    if result.stdout:
        print(result.stdout, end="")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
