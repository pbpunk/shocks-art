from __future__ import annotations

import json
import os
import subprocess
import sys
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


def main() -> int:
    expected = os.getenv("SHOCKS_HOST_EXPECTED_REVISION", "").strip().lower()
    live_root_value = os.getenv("SHOCKS_HOST_LIVE_ROOT", "").strip()
    if not expected or not live_root_value:
        return emit({"summary": "Host verifier did not provide the expected revision/live checkout contract"}, 2)
    live_root = Path(live_root_value).resolve()
    try:
        if git_revision(live_root) != expected:
            return emit({"summary": "Live checkout is not on the requested exact revision"}, 1)
        helper = live_root / "tools" / "retrieval_quality_live.py"
        result = subprocess.run(
            [sys.executable, str(helper)],
            cwd=live_root,
            env=os.environ.copy(),
            text=True,
            capture_output=True,
            timeout=1800,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            parsed = json.loads(result.stdout.strip().splitlines()[-1]) if result.stdout.strip() else {}
        except json.JSONDecodeError:
            parsed = {"summary": "retrieval_quality_live.py returned non-JSON output"}
        if result.stderr.strip():
            parsed["helper_stderr_present"] = True
        if result.returncode != 0:
            parsed.setdefault("summary", f"Retrieval quality helper exited with code {result.returncode}")
            return emit(parsed, 1)
        if git_revision(live_root) != expected:
            return emit({**parsed, "summary": "Live checkout moved during retrieval evaluation"}, 1)
        parsed["exact_live_revision_verified"] = True
        return emit(parsed)
    except subprocess.TimeoutExpired:
        return emit({"summary": "Retrieval quality evaluation exceeded its fixed execution budget", "timed_out": True}, 124)
    except Exception as exc:
        return emit({"summary": f"Retrieval quality evaluation failed before completion: {exc}"}, 1)


if __name__ == "__main__":
    raise SystemExit(main())
