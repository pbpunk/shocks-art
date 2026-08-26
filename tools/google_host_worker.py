from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build

from app.host_bridge import (
    STATE_HEADERS,
    TERMINAL_STATES,
    VERIFICATION_HEADERS,
    HostBridgeValidationError,
    parse_request_row,
    profile_policy,
    request_fingerprint,
)

ROOT = Path(__file__).resolve().parents[1]
SHEET_NAME = "Verification"
STATE_SHEET_NAME = "State"
SCHEMA_VERSION = 2
POLL_SECONDS = max(5, int(os.getenv("SHOCKS_GOOGLE_HOST_POLL_SECONDS", "15")))
SPREADSHEET_ID = os.getenv("SHOCKS_GOOGLE_SPREADSHEET_ID", "").strip()
CREDENTIALS_PATH = Path(os.getenv("SHOCKS_GOOGLE_CREDENTIALS", r"F:\JARVIS-secrets\pancake-google-service-account.json"))
JOURNAL_PATH = Path(os.getenv("SHOCKS_GOOGLE_HOST_JOURNAL_PATH", str(ROOT / "data" / "google_host_journal.json")))
STATUS_PATH = Path(os.getenv("SHOCKS_GOOGLE_HOST_STATUS_PATH", str(ROOT / "data" / "google_host_worker_status.json")))
WORKTREE_ROOT = Path(os.getenv("SHOCKS_GOOGLE_HOST_WORKTREE_ROOT", str(Path(tempfile.gettempdir()) / "shocks-art-host-worktrees")))
TIMEOUT_SECONDS = max(60, int(os.getenv("SHOCKS_GOOGLE_HOST_TIMEOUT_SECONDS", "21600")))
RESULT_LIMIT = 30000
stopping = False


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_json(path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else fallback
    except Exception:
        return fallback


def run(args: list[str], *, cwd: Path = ROOT, timeout: int = 120, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True, timeout=timeout, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def git(*args: str, cwd: Path = ROOT, timeout: int = 120, check: bool = True) -> str:
    result = run(["git", *args], cwd=cwd, timeout=timeout)
    if check and result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def local_revision(cwd: Path = ROOT) -> str:
    return git("rev-parse", "HEAD", cwd=cwd, timeout=10).lower()


def tracked_dirty(cwd: Path = ROOT) -> str:
    return git("status", "--porcelain", "--untracked-files=no", cwd=cwd, timeout=10)


def refresh_refs() -> str:
    git("fetch", "origin", "main", "--prune", timeout=120)
    git("fetch", "origin", "+refs/heads/autonomous/*:refs/remotes/origin/autonomous/*", "--prune", timeout=120)
    return git("rev-parse", "origin/main", timeout=10).lower()


def autonomous_tips() -> dict[str, str]:
    output = git("for-each-ref", "--format=%(refname:short) %(objectname)", "refs/remotes/origin/autonomous/", timeout=10)
    tips: dict[str, str] = {}
    for line in output.splitlines():
        if line.strip() and " " in line:
            ref, sha = line.split(" ", 1)
            tips[ref.strip()] = sha.strip().lower()
    return tips


def revision_allowed(expected: str, profile: str) -> tuple[bool, str]:
    main = refresh_refs()
    if expected == main:
        return True, "origin/main"
    if profile_policy(profile) == "main-only":
        return False, f"{profile} is main-only and requires the deployed origin/main revision"
    matching = [ref for ref, sha in autonomous_tips().items() if sha == expected]
    if not matching:
        return False, "revision is neither origin/main nor the exact tip of origin/autonomous/*"
    ancestor = run(["git", "merge-base", "--is-ancestor", main, expected], cwd=ROOT, timeout=10)
    if ancestor.returncode != 0:
        return False, "origin/main is not an ancestor of candidate revision"
    return True, matching[0]


def safe_worktree(request_id: str) -> Path:
    WORKTREE_ROOT.mkdir(parents=True, exist_ok=True)
    root = WORKTREE_ROOT.resolve()
    target = (root / request_id).resolve()
    if target == root or root not in target.parents:
        raise RuntimeError("unsafe worktree path")
    return target


def remove_worktree(target: Path) -> bool:
    run(["git", "worktree", "remove", "--force", str(target)], cwd=ROOT, timeout=60)
    run(["git", "worktree", "prune"], cwd=ROOT, timeout=60)
    shutil.rmtree(target, ignore_errors=True)
    return not target.exists()


def create_worktree(request_id: str, revision: str) -> Path:
    target = safe_worktree(request_id)
    remove_worktree(target)
    result = run(["git", "worktree", "add", "--detach", str(target), revision], cwd=ROOT, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "git worktree add failed")
    if local_revision(target) != revision or tracked_dirty(target):
        remove_worktree(target)
        raise RuntimeError("detached worktree failed exact-SHA/cleanliness validation")
    return target


def sheets_service():
    if not SPREADSHEET_ID:
        raise RuntimeError("SHOCKS_GOOGLE_SPREADSHEET_ID is not configured")
    if not CREDENTIALS_PATH.is_file():
        raise RuntimeError(f"Google service-account credentials not found: {CREDENTIALS_PATH}")
    credentials = service_account.Credentials.from_service_account_file(str(CREDENTIALS_PATH), scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return build("sheets", "v4", credentials=credentials, cache_discovery=False)


def ensure_schema(service) -> None:
    meta = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    titles = {str(s.get("properties", {}).get("title", "")) for s in meta.get("sheets", [])}
    missing = [name for name in (SHEET_NAME, STATE_SHEET_NAME) if name not in titles]
    if missing:
        service.spreadsheets().batchUpdate(spreadsheetId=SPREADSHEET_ID, body={"requests": [{"addSheet": {"properties": {"title": name}}} for name in missing]}).execute()
    update_values(service, f"{SHEET_NAME}!A1:N1", [list(VERIFICATION_HEADERS)])
    update_values(service, f"{STATE_SHEET_NAME}!A1:F1", [list(STATE_HEADERS)])


def update_values(service, range_name: str, values: list[list[Any]]) -> None:
    service.spreadsheets().values().update(spreadsheetId=SPREADSHEET_ID, range=range_name, valueInputOption="RAW", body={"values": values}).execute()


def read_rows(service) -> list[tuple[int, list[str]]]:
    response = service.spreadsheets().values().get(spreadsheetId=SPREADSHEET_ID, range=f"{SHEET_NAME}!A2:N1000").execute()
    return [(index + 2, list(row)) for index, row in enumerate(response.get("values", []))]


def normalized_row(row: list[str]) -> list[str]:
    values = [str(v or "") for v in row]
    values.extend([""] * max(0, len(VERIFICATION_HEADERS) - len(values)))
    return values[:len(VERIFICATION_HEADERS)]


def write_state(service, status: str, *, last_request: str = "", error: str = "", active: str = "") -> None:
    try:
        revision = local_revision()
    except Exception:
        revision = "unknown"
    update_values(service, f"{STATE_SHEET_NAME}!A2:F2", [[now_iso(), status, revision, last_request, error[:1500], active]])
    atomic_json(STATUS_PATH, {"schemaVersion": SCHEMA_VERSION, "process": "google_host_worker", "pid": os.getpid(), "status": status, "heartbeatTimestamp": now_iso(), "revision": revision, "lastRequestId": last_request, "lastError": error[:1500], "activeRequestId": active})


def result_row(request, *, state: str, started: str = "", finished: str = "", tested: str = "", outcome: str = "", exit_code: Any = "", duration: Any = "", summary: str = "", result_json: str = "") -> list[str]:
    return [request.request_id, request.created_at, request.expected_revision, request.profile, request.requester_id, state, started, finished, tested, outcome, str(exit_code), str(duration), summary[:1500], result_json[:RESULT_LIMIT]]


def write_row(service, row_number: int, values: list[str]) -> None:
    update_values(service, f"{SHEET_NAME}!A{row_number}:N{row_number}", [values])


def save_job(journal: dict[str, Any], request_id: str, fingerprint: list[str], state: str, row: list[str]) -> None:
    journal.setdefault("requests", {})[request_id] = {"fingerprint": fingerprint, "state": state, "row": row}
    atomic_json(JOURNAL_PATH, journal)


def recover_interrupted(journal: dict[str, Any]) -> None:
    changed = False
    for job in journal.setdefault("requests", {}).values():
        if job.get("state") != "running":
            continue
        row = normalized_row(job.get("row", []))
        row[5] = "failed"
        row[7] = now_iso()
        row[9] = "FAIL"
        row[10] = "1"
        row[12] = "worker restarted during host verification; request was not replayed"
        job["state"] = "failed"
        job["row"] = row
        changed = True
    if changed:
        atomic_json(JOURNAL_PATH, journal)


def execute_request(service, row_number: int, request, journal: dict[str, Any]) -> None:
    fingerprint = list(request_fingerprint(request))
    existing = journal.setdefault("requests", {}).get(request.request_id)
    if existing and existing.get("fingerprint") != fingerprint:
        write_row(service, row_number, result_row(request, state="rejected", finished=now_iso(), outcome="REJECT", summary="request_id was reused with different immutable inputs"))
        return
    if existing and existing.get("state") in TERMINAL_STATES:
        write_row(service, row_number, existing["row"])
        return

    allowed, ref_name = revision_allowed(request.expected_revision, request.profile)
    if not allowed:
        row = result_row(request, state="rejected", finished=now_iso(), outcome="REJECT", summary=ref_name)
        save_job(journal, request.request_id, fingerprint, "rejected", row)
        write_row(service, row_number, row)
        return

    started = now_iso()
    running_row = result_row(request, state="running", started=started)
    save_job(journal, request.request_id, fingerprint, "running", running_row)
    write_row(service, row_number, running_row)
    write_state(service, "running", last_request=request.request_id, active=request.request_id)
    target: Path | None = None
    started_clock = time.monotonic()
    try:
        target = create_worktree(request.request_id, request.expected_revision)
        env = os.environ.copy()
        env["SHOCKS_HOST_LIVE_ROOT"] = str(ROOT)
        env["SHOCKS_HOST_REQUEST_ID"] = request.request_id
        env["SHOCKS_HOST_EXPECTED_REVISION"] = request.expected_revision
        result = subprocess.run([sys.executable, "tools/run_host_profile.py", request.profile], cwd=target, env=env, text=True, capture_output=True, timeout=TIMEOUT_SECONDS, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        tested = local_revision(target)
        if tested != request.expected_revision or tracked_dirty(target):
            raise RuntimeError("tested worktree changed revision or became dirty")
        main_after = refresh_refs()
        if ref_name == "origin/main" and request.expected_revision != main_after:
            raise RuntimeError("origin/main moved during verification")
        if ref_name != "origin/main" and autonomous_tips().get(ref_name) != request.expected_revision:
            raise RuntimeError("candidate autonomous branch moved or disappeared during verification")
        stdout, stderr = result.stdout.strip(), result.stderr.strip()
        parsed: dict[str, Any] = {}
        if stdout:
            try:
                parsed = json.loads(stdout.splitlines()[-1])
            except Exception:
                parsed = {"stdout_tail": stdout[-6000:]}
        if stderr:
            parsed["stderr_tail"] = stderr[-6000:]
        outcome = "PASS" if result.returncode == 0 else "FAIL"
        summary = str(parsed.get("summary") or f"{request.profile} {outcome} on {ref_name}")
        row = result_row(request, state="completed" if result.returncode == 0 else "failed", started=started, finished=now_iso(), tested=tested, outcome=outcome, exit_code=result.returncode, duration=round(time.monotonic() - started_clock, 3), summary=summary, result_json=json.dumps(parsed, separators=(",", ":"), sort_keys=True))
    except Exception as exc:
        row = result_row(request, state="failed", started=started, finished=now_iso(), tested=request.expected_revision, outcome="FAIL", exit_code=1, duration=round(time.monotonic() - started_clock, 3), summary=str(exc))
    finally:
        cleanup_ok = True if target is None else remove_worktree(target)
        if not cleanup_ok:
            row[5] = "failed"; row[9] = "FAIL"; row[12] = (row[12] + "; detached worktree cleanup failed")[:1500]

    save_job(journal, request.request_id, fingerprint, row[5], row)
    write_row(service, row_number, row)
    write_state(service, "idle", last_request=request.request_id)


def process_once(service, journal: dict[str, Any]) -> None:
    for row_number, raw in read_rows(service):
        row = normalized_row(raw)
        if not row[0] or row[5].strip().lower() in TERMINAL_STATES:
            continue
        try:
            request = parse_request_row(row)
        except HostBridgeValidationError as exc:
            row[5] = "rejected"; row[7] = now_iso(); row[9] = "REJECT"; row[12] = str(exc)[:1500]
            write_row(service, row_number, row)
            continue
        existing = journal.setdefault("requests", {}).get(request.request_id)
        if row[5].strip().lower() == "running" and not existing:
            failed = result_row(request, state="failed", started=row[6], finished=now_iso(), tested=request.expected_revision, outcome="FAIL", exit_code=1, summary="orphaned running Sheet row has no durable journal entry; request was not replayed")
            save_job(journal, request.request_id, list(request_fingerprint(request)), "failed", failed)
            write_row(service, row_number, failed)
            continue
        execute_request(service, row_number, request, journal)
        return


def main() -> int:
    global stopping
    def stop_handler(*_args):
        global stopping
        stopping = True
    signal.signal(signal.SIGINT, stop_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop_handler)

    journal = load_json(JOURNAL_PATH, {"schemaVersion": SCHEMA_VERSION, "requests": {}})
    journal.setdefault("requests", {})
    journal["schemaVersion"] = SCHEMA_VERSION
    recover_interrupted(journal)
    atomic_json(JOURNAL_PATH, journal)

    service = sheets_service()
    ensure_schema(service)
    write_state(service, "idle")
    while not stopping:
        try:
            process_once(service, journal)
            write_state(service, "idle")
        except Exception as exc:
            write_state(service, "error", error=f"{type(exc).__name__}: {exc}")
        for _ in range(POLL_SECONDS):
            if stopping:
                break
            time.sleep(1)
    write_state(service, "stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
