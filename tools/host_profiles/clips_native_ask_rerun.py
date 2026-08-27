from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

TARGET_VIDEO_ID = "pDC14ymQqWY"
FORBIDDEN_TITLE = "Studio Tour and Finished Pieces"


def emit(payload: dict[str, Any], code: int = 0) -> int:
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    return code


def git_revision(root: Path) -> str:
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True, timeout=15, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode != 0:
        raise RuntimeError("could not read live Git revision")
    return result.stdout.strip().lower()


def http_request(url: str, *, timeout: int = 20) -> tuple[int, str]:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def live_python(live_root: Path) -> Path:
    for candidate in (live_root / ".venv" / "Scripts" / "python.exe", Path(sys.executable)):
        if not candidate.exists():
            continue
        probe = subprocess.run([str(candidate), "-c", "import sqlalchemy, playwright"], cwd=live_root, text=True, capture_output=True, timeout=30, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if probe.returncode == 0:
            return candidate
    raise RuntimeError("no live Python runtime has both SQLAlchemy and Playwright available")


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
        base_url = str(manifest["local"]["baseUrl"]).rstrip("/")
        health_status, _ = http_request(str(manifest["health"]["url"]))
        if health_status != 200:
            return emit({"summary": f"Live app health returned HTTP {health_status}"}, 1)
        python = live_python(live_root)
        helper = live_root / "tools" / "clips_native_ask_rerun_live.py"
        result = subprocess.run([str(python), str(helper)], cwd=live_root, env=os.environ.copy(), text=True, capture_output=True, timeout=900, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        parsed: dict[str, Any] = {}
        if result.stdout.strip():
            try:
                parsed = json.loads(result.stdout.strip().splitlines()[-1])
            except json.JSONDecodeError:
                parsed = {"summary": "Single-video rerun returned non-JSON output"}
        if result.stderr.strip():
            parsed["helper_stderr_present"] = True
        if result.returncode != 0:
            parsed.setdefault("summary", f"Single-video rerun exited with code {result.returncode}")
            return emit(parsed, 1)
        if parsed.get("source_video_id") != TARGET_VIDEO_ID:
            return emit({**parsed, "summary": "Rerun targeted the wrong YouTube video"}, 1)
        titles = [str(item.get("title") or "") for item in parsed.get("candidates") or []]
        if any(FORBIDDEN_TITLE.lower() in title.lower() for title in titles):
            return emit({**parsed, "summary": f"Stale Ask context leaked again: {FORBIDDEN_TITLE}"}, 1)
        page_status, page_html = http_request(f"{base_url}/", timeout=30)
        if page_status != 200:
            return emit({**parsed, "summary": f"Production Clips page returned HTTP {page_status}"}, 1)
        missing = [cid for cid in parsed.get("candidate_window_ids") or [] if f'data-candidate-id="{cid}"' not in page_html]
        if missing:
            return emit({**parsed, "summary": "New rerun candidates were not visible in production Clips", "missing_from_feed": missing}, 1)
        if git_revision(live_root) != expected:
            return emit({**parsed, "summary": "Live checkout moved during the single-video rerun"}, 1)
        parsed["summary"] = f"Fractal Burning rerun passed: {len(parsed.get('candidates') or [])} fresh native-Ask candidate(s); no stale Studio Tour context"
        parsed["production_feed_verified"] = True
        return emit(parsed)
    except subprocess.TimeoutExpired:
        return emit({"summary": "Fractal Burning rerun exceeded its fixed live-browser budget", "timed_out": True}, 124)
    except Exception as exc:
        return emit({"summary": f"Fractal Burning rerun failed before completion: {exc}"}, 1)


if __name__ == "__main__":
    raise SystemExit(main())
