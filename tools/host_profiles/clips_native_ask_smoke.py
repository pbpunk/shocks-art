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


def http_request(url: str, *, method: str = "GET", timeout: int = 20) -> tuple[int, str]:
    request = urllib.request.Request(
        url,
        data=b"" if method == "POST" else None,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def live_python(live_root: Path) -> Path:
    candidates = [live_root / ".venv" / "Scripts" / "python.exe", Path(sys.executable)]
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate).lower()
        if key in seen or not candidate.exists():
            continue
        seen.add(key)
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
    raise RuntimeError("no live Python runtime has both SQLAlchemy and Playwright available")


def main() -> int:
    expected = os.getenv("SHOCKS_HOST_EXPECTED_REVISION", "").strip().lower()
    live_root_value = os.getenv("SHOCKS_HOST_LIVE_ROOT", "").strip()
    if not expected or not live_root_value:
        return emit({"summary": "Host verifier did not provide the expected revision/live checkout contract"}, 2)

    live_root = Path(live_root_value).resolve()
    try:
        revision_before = git_revision(live_root)
        if revision_before != expected:
            return emit(
                {
                    "summary": f"Live Shocks Art checkout is {revision_before}, expected {expected}",
                    "live_revision": revision_before,
                    "expected_revision": expected,
                },
                1,
            )

        manifest = json.loads((live_root / "jarvis.app.json").read_text(encoding="utf-8"))
        base_url = str(manifest["local"]["baseUrl"]).rstrip("/")
        health_url = str(manifest["health"]["url"])

        health_status, _ = http_request(health_url)
        if health_status != 200:
            return emit({"summary": f"Live app health returned HTTP {health_status}"}, 1)

        legacy_results: dict[str, int] = {}
        for path in ("/api/process", "/actions/process-one", "/api/streams/smoke/analyze"):
            status, body = http_request(f"{base_url}{path}", method="POST")
            legacy_results[path] = status
            if status != 410 or "Direct Gemini video analysis is disabled" not in body:
                return emit(
                    {
                        "summary": f"Legacy direct-Gemini guard failed for {path}: HTTP {status}",
                        "legacy_route_statuses": legacy_results,
                    },
                    1,
                )

        python = live_python(live_root)
        helper = live_root / "tools" / "clips_native_ask_smoke_live.py"
        if not helper.is_file():
            return emit({"summary": "Live native Ask smoke helper is missing"}, 1)

        result = subprocess.run(
            [str(python), str(helper)],
            cwd=live_root,
            env=os.environ.copy(),
            text=True,
            capture_output=True,
            timeout=900,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        stdout = result.stdout.strip()
        parsed: dict[str, Any] = {}
        if stdout:
            try:
                parsed = json.loads(stdout.splitlines()[-1])
            except json.JSONDecodeError:
                parsed = {"summary": "Live native Ask smoke helper returned non-JSON output"}
        if result.stderr.strip():
            parsed["helper_stderr_present"] = True
        if result.returncode != 0:
            parsed.setdefault("summary", f"Live native Ask smoke helper exited with code {result.returncode}")
            parsed["helper_exit_code"] = result.returncode
            parsed["legacy_route_statuses"] = legacy_results
            return emit(parsed, 1)

        candidate_ids = [str(value) for value in parsed.get("candidate_window_ids") or []]
        page_status, page_html = http_request(f"{base_url}/", timeout=30)
        if page_status != 200:
            return emit({"summary": f"Production Clips page returned HTTP {page_status}", **parsed}, 1)
        missing = [candidate_id for candidate_id in candidate_ids if f'data-candidate-id="{candidate_id}"' not in page_html]
        if missing:
            return emit(
                {
                    **parsed,
                    "summary": f"Native-Ask candidate(s) were persisted but missing from the production Clips feed: {', '.join(missing)}",
                    "missing_from_feed": missing,
                },
                1,
            )

        revision_after = git_revision(live_root)
        if revision_after != expected:
            return emit(
                {
                    **parsed,
                    "summary": f"Live checkout moved during smoke from {expected} to {revision_after}",
                    "live_revision_after": revision_after,
                },
                1,
            )

        parsed["summary"] = (
            f"Native YouTube Ask smoke passed on {parsed.get('source_video_id')}: "
            f"{len(candidate_ids)} production clip candidate(s), direct Gemini blocked"
        )
        parsed["live_revision"] = revision_after
        parsed["legacy_route_statuses"] = legacy_results
        parsed["production_feed_verified"] = True
        return emit(parsed)
    except subprocess.TimeoutExpired:
        return emit({"summary": "Native YouTube Ask smoke exceeded its fixed 900-second live-browser budget", "timed_out": True}, 124)
    except Exception as exc:
        return emit({"summary": f"Native YouTube Ask smoke failed before completion: {exc}"}, 1)


if __name__ == "__main__":
    raise SystemExit(main())
