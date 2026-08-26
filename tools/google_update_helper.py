from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build

ROOT = Path(__file__).resolve().parents[1]
UPDATE_CMD = ROOT / "Update App.cmd"
SPREADSHEET_ID = os.getenv("SHOCKS_GOOGLE_SPREADSHEET_ID", "1hD8IqH_o1RJnVyxpAuhSmR-nB6DNwvK51xZIyn8IX_I").strip()
CREDENTIALS_PATH = Path(os.getenv("SHOCKS_GOOGLE_CREDENTIALS", r"F:\JARVIS-secrets\pancake-google-service-account.json"))
RECEIPT_DIR = Path(os.getenv("SHOCKS_GOOGLE_UPDATE_RECEIPT_DIR", str(ROOT / "data" / "google_update_receipts")))
LOG_PATH = ROOT / "data" / "logs" / "google_update_helper.log"
UPDATES_HEADERS = (
    "request_id", "created_at", "expected_revision", "requester_id", "state",
    "launched_at", "finished_at", "running_revision", "outcome", "error",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


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


def validate_request_id(value: str) -> str:
    import re

    request_id = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", request_id):
        raise ValueError("invalid request_id")
    return request_id


def normalize_revision(value: str) -> str:
    import re

    revision = str(value or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("expected_revision must be a full 40-character Git SHA")
    return revision


def local_revision() -> str:
    return git("rev-parse", "HEAD", timeout=10)


def tracked_dirty() -> bool:
    return bool(git("status", "--porcelain", "--untracked-files=no", timeout=10, check=False))


def preflight(expected: str) -> dict[str, str]:
    git("fetch", "origin", "main", "--prune", timeout=120)
    current = local_revision()
    main = git("rev-parse", "origin/main", timeout=10)
    if tracked_dirty():
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
    credentials = service_account.Credentials.from_service_account_file(
        str(CREDENTIALS_PATH), scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return build("sheets", "v4", credentials=credentials, cache_discovery=False)


def publish_receipt(request_id: str, receipt: dict[str, Any]) -> None:
    service = sheets_service()
    rows = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID, range="Updates!A2:J1000"
    ).execute().get("values", [])
    for index, row in enumerate(rows, start=2):
        if str(row[0] if row else "").strip() != request_id:
            continue
        values = [
            request_id,
            str(receipt.get("created_at", "")),
            str(receipt.get("expected_revision", "")),
            str(receipt.get("requester_id", "")),
            str(receipt.get("state", "")),
            str(receipt.get("launched_at", "")),
            str(receipt.get("finished_at", "")),
            str(receipt.get("running_revision", "")),
            str(receipt.get("outcome", "")),
            str(receipt.get("error", ""))[:1500],
        ]
        service.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=f"Updates!A{index}:J{index}",
            valueInputOption="RAW",
            body={"values": [values]},
        ).execute()
        return


def write_receipt(request_id: str, receipt: dict[str, Any]) -> None:
    atomic_json(RECEIPT_DIR / f"{request_id}.json", receipt)


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("usage: google_update_helper.py REQUEST_ID EXPECTED_REVISION")
    request_id = validate_request_id(sys.argv[1])
    expected = normalize_revision(sys.argv[2])
    launched_at = now_iso()
    base = {
        "request_id": request_id,
        "created_at": os.getenv("SHOCKS_GOOGLE_UPDATE_CREATED_AT", ""),
        "expected_revision": expected,
        "requester_id": os.getenv("SHOCKS_GOOGLE_UPDATE_REQUESTER_ID", ""),
        "launched_at": launched_at,
    }

    try:
        check = preflight(expected)
        if check["decision"] == "already_running":
            receipt = {**base, "state": "completed", "finished_at": now_iso(), "running_revision": check["current"], "outcome": "already_running", "error": ""}
            write_receipt(request_id, receipt)
            try: publish_receipt(request_id, receipt)
            except Exception: pass
            return 0
        if check["decision"] != "launch":
            receipt = {**base, "state": "rejected", "finished_at": now_iso(), "running_revision": check.get("current", ""), "outcome": "preflight_rejected", "error": check.get("reason", "preflight rejected")}
            write_receipt(request_id, receipt)
            try: publish_receipt(request_id, receipt)
            except Exception: pass
            return 2
        if not UPDATE_CMD.is_file():
            raise RuntimeError(f"canonical updater missing: {UPDATE_CMD}")

        running = {**base, "state": "running", "finished_at": "", "running_revision": check["current"], "outcome": "updater_running", "error": ""}
        write_receipt(request_id, running)
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as log:
            log.write(f"\n{now_iso()} request={request_id} expected={expected}\n")
            log.flush()
            result = subprocess.run(
                ["cmd.exe", "/d", "/c", str(UPDATE_CMD)],
                cwd=ROOT,
                stdout=log,
                stderr=log,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        deployed = local_revision()
        exact = deployed == expected
        receipt = {
            **base,
            "state": "completed" if result.returncode == 0 and exact else "failed",
            "finished_at": now_iso(),
            "running_revision": deployed,
            "outcome": "updated_exact" if result.returncode == 0 and exact else "unexpected_revision" if result.returncode == 0 else "updater_failed",
            "error": "" if result.returncode == 0 and exact else (f"canonical updater exited with code {result.returncode}" if result.returncode != 0 else "running revision does not exactly equal requested revision"),
        }
    except Exception as exc:
        try: deployed = local_revision()
        except Exception: deployed = ""
        receipt = {**base, "state": "failed", "finished_at": now_iso(), "running_revision": deployed, "outcome": "helper_exception", "error": f"{type(exc).__name__}: {exc}"}

    write_receipt(request_id, receipt)
    try:
        publish_receipt(request_id, receipt)
    except Exception as exc:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as log:
            log.write(f"{now_iso()} receipt publish failed: {type(exc).__name__}: {exc}\n")
    return 0 if receipt["state"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
