from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
TIMEOUT_SECONDS = max(60, int(os.getenv("SHOCKS_REPO_TEST_TIMEOUT_SECONDS", "1800")))


def emit(payload: dict[str, Any], code: int = 0) -> int:
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    return code


def _works(command: list[str]) -> bool:
    try:
        result = subprocess.run(
            [*command, "-c", "import pytest, app.main"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return result.returncode == 0
    except Exception:
        return False


def _python_command() -> list[str] | None:
    explicit = os.getenv("SHOCKS_TEST_PYTHON", "").strip()
    if explicit and _works([explicit]):
        return [explicit]

    venv_python = ROOT / ".venv" / "Scripts" / "python.exe"
    if venv_python.is_file() and _works([str(venv_python)]):
        return [str(venv_python)]

    candidates = (["py", "-3.13"], ["py", "-3"], ["python"])
    for candidate in candidates:
        if _works(list(candidate)):
            return list(candidate)
    return None


def main() -> int:
    python = _python_command()
    if not python:
        return emit(
            {
                "summary": "No Python runtime with pytest and Shock's Art app dependencies is available",
                "configured": False,
            },
            2,
        )

    started = time.monotonic()
    try:
        result = subprocess.run(
            [*python, "-m", "pytest", "-q"],
            cwd=ROOT,
            env=os.environ.copy(),
            text=True,
            capture_output=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        duration = time.monotonic() - started
        return emit(
            {
                "summary": "Repository test suite timed out",
                "duration_seconds": round(duration, 3),
                "timeout_seconds": TIMEOUT_SECONDS,
                "stdout_tail": str(exc.stdout or "")[-4000:],
                "stderr_tail": str(exc.stderr or "")[-4000:],
            },
            1,
        )

    duration = time.monotonic() - started
    stdout = result.stdout or ""
    stderr = result.stderr or ""
    tail = "\n".join(stdout.strip().splitlines()[-12:])
    summary = tail.splitlines()[-1] if tail else (stderr.strip().splitlines()[-1] if stderr.strip() else "pytest completed")
    return emit(
        {
            "summary": summary,
            "duration_seconds": round(duration, 3),
            "exit_code": result.returncode,
            "python": " ".join(python),
            "stdout_tail": tail[-6000:],
            "stderr_tail": stderr[-4000:],
        },
        0 if result.returncode == 0 else 1,
    )


if __name__ == "__main__":
    raise SystemExit(main())
