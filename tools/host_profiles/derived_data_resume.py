from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def emit(payload: dict[str, Any], code: int = 0) -> int:
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    return code


def git_revision(root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        raise RuntimeError("could not read live Git revision")
    return result.stdout.strip().lower()


def http_status(url: str, timeout: int = 20) -> int:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def live_python(live_root: Path) -> Path:
    for candidate in (live_root / ".venv" / "Scripts" / "python.exe", Path(sys.executable)):
        if not candidate.exists():
            continue
        probe = subprocess.run(
            [str(candidate), "-c", "import sqlalchemy, playwright"],
            cwd=live_root,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if probe.returncode == 0:
            return candidate
    raise RuntimeError("no live Python runtime has the required SQLAlchemy/Playwright dependencies")


def main() -> int:
    expected = os.getenv("SHOCKS_HOST_EXPECTED_REVISION", "").strip().lower()
    live_root_value = os.getenv("SHOCKS_HOST_LIVE_ROOT", "").strip()
    if not expected or not live_root_value:
        return emit({"summary": "Host verifier did not provide the expected revision/live checkout contract"}, 2)
    live_root = Path(live_root_value).resolve()
    try:
        if git_revision(live_root) != expected:
            return emit({"summary": "Live checkout is not on the requested exact revision"}, 1)
        manifest = json.loads((live_root / "jarvis.app.json").read_text(encoding="utf-8"))
        health_url = str(manifest["health"]["url"])
        if http_status(health_url) != 200:
            return emit({"summary": "Live app health is not HTTP 200 before derived-data resume"}, 1)

        python = live_python(live_root)
        helper = live_root / "tools" / "resume_derived_data_live.py"
        result = subprocess.run(
            [str(python), str(helper)],
            cwd=live_root,
            env=os.environ.copy(),
            text=True,
            capture_output=True,
            timeout=6600,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            parsed = json.loads(result.stdout.strip().splitlines()[-1]) if result.stdout.strip() else {}
        except json.JSONDecodeError:
            parsed = {"summary": "resume_derived_data_live.py returned non-JSON output"}
        if result.stderr.strip():
            parsed["helper_stderr_present"] = True
        if result.returncode != 0:
            parsed.setdefault("summary", f"Derived-data resume helper exited with code {result.returncode}")
            return emit(parsed, 1)
        if git_revision(live_root) != expected:
            return emit({**parsed, "summary": "Live checkout moved during derived-data resume"}, 1)
        if http_status(health_url) != 200:
            return emit({**parsed, "summary": "Live app health is not HTTP 200 after derived-data resume"}, 1)
        parsed["exact_live_revision_verified"] = True
        return emit(parsed)
    except subprocess.TimeoutExpired:
        return emit({"summary": "Derived-data resume exceeded its fixed execution budget", "timed_out": True}, 124)
    except Exception as exc:
        return emit({"summary": f"Derived-data resume failed before completion: {exc}"}, 1)


if __name__ == "__main__":
    raise SystemExit(main())
