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


def run_json_helper(python: Path, helper: Path, live_root: Path, *, timeout: int) -> tuple[int, dict[str, Any], bool]:
    result = subprocess.run(
        [str(python), str(helper)],
        cwd=live_root,
        env=os.environ.copy(),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        parsed = json.loads(result.stdout.strip().splitlines()[-1]) if result.stdout.strip() else {}
    except json.JSONDecodeError:
        parsed = {"summary": f"{helper.name} returned non-JSON output"}
    return result.returncode, parsed, bool(result.stderr.strip())


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
            return emit({"summary": "Live app health is not HTTP 200 before reinitialization"}, 1)

        python = live_python(live_root)
        preclear_helper = live_root / "tools" / "prepare_derived_data_reinitialize.py"
        preclear_code, preclear, preclear_stderr = run_json_helper(
            python,
            preclear_helper,
            live_root,
            timeout=180,
        )
        if preclear_stderr:
            preclear["helper_stderr_present"] = True
        if preclear_code != 0:
            preclear.setdefault("summary", f"Derived-data pre-clear exited with code {preclear_code}")
            return emit(preclear, 1)

        helper = live_root / "tools" / "reinitialize_derived_data_live.py"
        result_code, parsed, helper_stderr = run_json_helper(
            python,
            helper,
            live_root,
            timeout=6600,
        )
        if helper_stderr:
            parsed["helper_stderr_present"] = True
        parsed["unpublished_derived_assets_removed"] = int(preclear.get("derived_assets_removed") or 0)
        parsed["preclear_backup_file"] = str(preclear.get("preclear_backup_file") or "")
        if result_code != 0:
            parsed.setdefault("summary", f"Derived-data helper exited with code {result_code}")
            return emit(parsed, 1)
        if git_revision(live_root) != expected:
            return emit({**parsed, "summary": "Live checkout moved during derived-data reinitialization"}, 1)
        if http_status(health_url) != 200:
            return emit({**parsed, "summary": "Live app health is not HTTP 200 after reinitialization"}, 1)
        after = parsed.get("after") or {}
        if int(after.get("directOrLegacyAnalysisRuns") or 0) != 0:
            return emit({**parsed, "summary": "Direct/legacy Gemini analysis lineage remains after reinitialization"}, 1)
        parsed["exact_live_revision_verified"] = True
        return emit(parsed)
    except subprocess.TimeoutExpired:
        return emit({"summary": "Derived-data reinitialization exceeded its fixed execution budget", "timed_out": True}, 124)
    except Exception as exc:
        return emit({"summary": f"Derived-data reinitialization failed before completion: {exc}"}, 1)


if __name__ == "__main__":
    raise SystemExit(main())
