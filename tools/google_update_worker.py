from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build

ROOT = Path(__file__).resolve().parents[1]
SPREADSHEET_ID = os.getenv("SHOCKS_GOOGLE_SPREADSHEET_ID", "1hD8IqH_o1RJnVyxpAuhSmR-nB6DNwvK51xZIyn8IX_I").strip()
CREDENTIALS_PATH = Path(os.getenv("SHOCKS_GOOGLE_CREDENTIALS", r"F:\JARVIS-secrets\pancake-google-service-account.json"))
POLL_SECONDS = max(5, int(os.getenv("SHOCKS_GOOGLE_UPDATE_POLL_SECONDS", "15")))
JOURNAL_PATH = Path(os.getenv("SHOCKS_GOOGLE_UPDATE_JOURNAL_PATH", str(ROOT / "data" / "google_update_journal.json")))
STATUS_PATH = Path(os.getenv("SHOCKS_GOOGLE_UPDATE_STATUS_PATH", str(ROOT / "data" / "google_update_worker_status.json")))
RECEIPT_DIR = Path(os.getenv("SHOCKS_GOOGLE_UPDATE_RECEIPT_DIR", str(ROOT / "data" / "google_update_receipts")))
HELPER_PATH = ROOT / "tools" / "google_update_helper.py"
UPDATES_HEADERS = (
    "request_id", "created_at", "expected_revision", "requester_id", "state",
    "launched_at", "finished_at", "running_revision", "outcome", "error",
)
UPDATE_STATE_HEADERS = ("update_updated_at", "update_status", "update_last_request_id", "update_last_error")
TERMINAL = {"completed", "failed", "rejected", "superseded"}
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


def run(args: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def git(*args: str, timeout: int = 120, check: bool = True) -> str:
    result = run(["git", *args], timeout=timeout)
    if check and result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip().lower()


def validate_request_id(value: object) -> str:
    import re

    request_id = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", request_id):
        raise ValueError("request_id must be 1-80 chars using letters, digits, '.', '_' or '-'")
    return request_id


def normalize_revision(value: object) -> str:
    import re

    revision = str(value or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("expected_revision must be a full 40-character Git SHA")
    return revision


def local_revision() -> str:
    return git("rev-parse", "HEAD", timeout=10)


def preflight(expected: str) -> dict[str, str]:
    git("fetch", "origin", "main", "--prune", timeout=120)
    current = local_revision()
    main = git("rev-parse", "origin/main", timeout=10)
    dirty = git("status", "--porcelain", "--untracked-files=no", timeout=10, check=False)
    if dirty:
        return {"decision": "reject", "reason": "tracked working tree is dirty", "current": current, "main": main}
    if expected != main:
        return {"decision": "reject", "reason": "expected_revision is not the exact current origin/main", "current": current, "main": main}
    if current == expected:
        return {"decision": "already_running", "reason": "requested revision is already deployed", "current": current, "main": main}
    ancestor = run(["git", "merge-base", "--is-ancestor", current, expected], timeout=10)
    if ancestor.returncode != 0:
        return {"decision": "reject", "reason": "current revision is not an ancestor of requested origin/main; refusing rollback/divergence", "current": current, "main": main}
    return {"decision": "launch", "reason": "", "current": current, "main": main}


def sheets_service():
    if not CREDENTIALS_PATH.is_file():
        raise RuntimeError(f"Google service-account credentials not found: {CREDENTIALS_PATH}")
    credentials = service_account.Credentials.from_service_account_file(
        str(CREDENTIALS_PATH), scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return build("sheets", "v4", credentials=credentials, cache_discovery=False)


def update_values(service, range_name: str, values: list[list[Any]]) -> None:
    service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=range_name,
        valueInputOption="RAW",
        body={"values": values},
    ).execute()


def ensure_schema(service) -> None:
    meta = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    titles = {str(s.get("properties", {}).get("title", "")) for s in meta.get("sheets", [])}
    if "Updates" not in titles:
        service.spreadsheets().batchUpdate(
            spreadsheetId=SPREADSHEET_ID,
            body={"requests": [{"addSheet": {"properties": {"title": "Updates"}}}]},
        ).execute()
    update_values(service, "Updates!A1:J1", [list(UPDATES_HEADERS)])
    update_values(service, "State!G1:J1", [list(UPDATE_STATE_HEADERS)])


def write_status(service, status: str, *, request_id: str = "", error: str = "") -> None:
    payload = {
        "process": "google_update_worker",
        "pid": os.getpid(),
        "status": status,
        "heartbeatTimestamp": now_iso(),
        "revision": local_revision() if (ROOT / ".git").exists() else "unknown",
        "lastRequestId": request_id,
        "lastError": error[:1500],
    }
    atomic_json(STATUS_PATH, payload)
    update_values(service, "State!G2:J2", [[payload["heartbeatTimestamp"], status, request_id, error[:1500]]])


def normalized(row: list[Any]) -> list[str]:
    values = [str(v or "") for v in row]
    values.extend([""] * max(0, 10 - len(values)))
    return values[:10]


def read_rows(service) -> list[tuple[int, list[str]]]:
    rows = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID, range="Updates!A2:J1000"
    ).execute().get("values", [])
    return [(index + 2, normalized(row)) for index, row in enumerate(rows)]


def write_row(service, row_number: int, row: list[str]) -> None:
    update_values(service, f"Updates!A{row_number}:J{row_number}", [normalized(row)])


def receipt_path(request_id: str) -> Path:
    return RECEIPT_DIR / f"{request_id}.json"


def read_receipt(request_id: str) -> dict[str, Any] | None:
    path = receipt_path(request_id)
    if not path.is_file():
        return None
    value = load_json(path, {})
    return value if value else None


def receipt_row(existing: list[str], receipt: dict[str, Any]) -> list[str]:
    return [
        str(receipt.get("request_id") or existing[0]),
        str(receipt.get("created_at") or existing[1]),
        str(receipt.get("expected_revision") or existing[2]),
        str(receipt.get("requester_id") or existing[3]),
        str(receipt.get("state") or existing[4]),
        str(receipt.get("launched_at") or existing[5]),
        str(receipt.get("finished_at") or existing[6]),
        str(receipt.get("running_revision") or existing[7]),
        str(receipt.get("outcome") or existing[8]),
        str(receipt.get("error") or existing[9])[:1500],
    ]


def launch_helper(request_id: str, expected: str, created_at: str, requester_id: str) -> int:
    env = os.environ.copy()
    env["SHOCKS_GOOGLE_UPDATE_CREATED_AT"] = created_at
    env["SHOCKS_GOOGLE_UPDATE_REQUESTER_ID"] = requester_id
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    child = subprocess.Popen(
        [sys.executable, str(HELPER_PATH), request_id, expected],
        cwd=ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=flags,
    )
    return int(child.pid)


def process_once(service, journal: dict[str, Any]) -> str:
    rows = read_rows(service)
    requests = journal.setdefault("requests", {})

    for row_number, row in rows:
        if not row[0]:
            continue
        try:
            request_id = validate_request_id(row[0])
            expected = normalize_revision(row[2])
        except Exception as exc:
            row[4], row[6], row[8], row[9] = "rejected", now_iso(), "invalid_request", str(exc)[:1500]
            write_row(service, row_number, row)
            continue

        state = row[4].strip().lower()
        record = requests.get(request_id)
        fingerprint = [request_id, expected]
        if record and record.get("fingerprint") != fingerprint:
            row[4], row[6], row[8], row[9] = "rejected", now_iso(), "request_id_reused", "request_id was reused with a different revision"
            write_row(service, row_number, row)
            continue

        receipt = read_receipt(request_id)
        if receipt and str(receipt.get("state", "")) in TERMINAL:
            terminal_row = receipt_row(row, receipt)
            requests[request_id] = {"fingerprint": fingerprint, "state": terminal_row[4], "row": terminal_row}
            atomic_json(JOURNAL_PATH, journal)
            write_row(service, row_number, terminal_row)
            continue
        if state in TERMINAL:
            requests.setdefault(request_id, {"fingerprint": fingerprint, "state": state, "row": row})
            continue
        if state in {"launched", "running"}:
            requests.setdefault(request_id, {"fingerprint": fingerprint, "state": state, "row": row})
            continue

        check = preflight(expected)
        if check["decision"] == "already_running":
            row[4], row[6], row[7], row[8], row[9] = "completed", now_iso(), check["current"], "already_running", ""
            requests[request_id] = {"fingerprint": fingerprint, "state": "completed", "row": row}
            atomic_json(JOURNAL_PATH, journal)
            write_row(service, row_number, row)
            return request_id
        if check["decision"] != "launch":
            row[4], row[6], row[7], row[8], row[9] = "rejected", now_iso(), check.get("current", ""), "preflight_rejected", check.get("reason", "preflight rejected")[:1500]
            requests[request_id] = {"fingerprint": fingerprint, "state": "rejected", "row": row}
            atomic_json(JOURNAL_PATH, journal)
            write_row(service, row_number, row)
            return request_id

        row[4] = "launched"
        row[5] = now_iso()
        row[7] = check["current"]
        row[8] = "helper_launched"
        row[9] = ""
        write_row(service, row_number, row)
        pid = launch_helper(request_id, expected, row[1], row[3])
        requests[request_id] = {"fingerprint": fingerprint, "state": "launched", "row": row, "helper_pid": pid}
        atomic_json(JOURNAL_PATH, journal)
        return request_id
    return ""


def main() -> int:
    global stopping

    def stop_handler(*_args):
        global stopping
        stopping = True

    signal.signal(signal.SIGINT, stop_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop_handler)

    journal = load_json(JOURNAL_PATH, {"schemaVersion": 1, "requests": {}})
    service = sheets_service()
    ensure_schema(service)
    write_status(service, "idle")
    while not stopping:
        try:
            request_id = process_once(service, journal)
            write_status(service, "idle", request_id=request_id)
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            try:
                write_status(service, "error", error=message)
            except Exception:
                atomic_json(STATUS_PATH, {"process": "google_update_worker", "status": "error", "heartbeatTimestamp": now_iso(), "lastError": message})
        for _ in range(POLL_SECONDS):
            if stopping:
                break
            time.sleep(1)
    try:
        write_status(service, "stopped")
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
