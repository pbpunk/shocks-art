from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build

LIVE_ROOT = Path(os.getenv("SHOCKS_HOST_LIVE_ROOT", Path(__file__).resolve().parents[2])).resolve()
if str(LIVE_ROOT) not in sys.path:
    sys.path.insert(0, str(LIVE_ROOT))

from tools.host_profiles.whisper_benchmark import validate_manifest_payload

BENCHMARK_DIR = LIVE_ROOT / "data" / "whisper_benchmark"
DRAFT_MANIFEST = BENCHMARK_DIR / "manifest.draft.json"
REVIEWED_MANIFEST = BENCHMARK_DIR / "manifest.json"
SPREADSHEET_ID = os.getenv("SHOCKS_GOOGLE_SPREADSHEET_ID", "").strip()
CREDENTIALS_PATH = Path(
    os.getenv(
        "SHOCKS_GOOGLE_CREDENTIALS",
        r"F:\JARVIS-secrets\pancake-google-service-account.json",
    )
)
REVIEW_RANGE = "'Whisper Review'!A2:J20"
REVIEWED_STATUS = "Reviewed"


def emit(payload: dict[str, Any], code: int = 0) -> int:
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    return code


def _normalized_rows(rows: list[list[object]]) -> list[list[str]]:
    normalized: list[list[str]] = []
    for raw in rows:
        values = [str(value or "").strip() for value in raw]
        values.extend([""] * max(0, 10 - len(values)))
        values = values[:10]
        if values[0]:
            normalized.append(values)
    return normalized


def build_reviewed_manifest(draft_payload: Any, review_rows: list[list[object]]) -> dict[str, Any]:
    draft_cases = draft_payload.get("cases", []) if isinstance(draft_payload, dict) else []
    if not isinstance(draft_cases, list) or not draft_cases:
        raise ValueError("review draft must contain a non-empty cases array")

    draft_by_id: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(draft_cases, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"draft case {index} must be an object")
        case_id = str(raw.get("id") or "").strip()
        if not case_id:
            raise ValueError(f"draft case {index} has an empty id")
        if case_id in draft_by_id:
            raise ValueError(f"duplicate draft case id: {case_id}")
        draft_by_id[case_id] = raw

    rows = _normalized_rows(review_rows)
    review_by_id: dict[str, list[str]] = {}
    for row in rows:
        case_id = row[0]
        if case_id in review_by_id:
            raise ValueError(f"duplicate review case id: {case_id}")
        review_by_id[case_id] = row

    draft_ids = set(draft_by_id)
    review_ids = set(review_by_id)
    missing = sorted(draft_ids - review_ids)
    extra = sorted(review_ids - draft_ids)
    if missing or extra:
        detail = []
        if missing:
            detail.append(f"missing review rows: {', '.join(missing)}")
        if extra:
            detail.append(f"unexpected review rows: {', '.join(extra)}")
        raise ValueError("; ".join(detail))

    cases: list[dict[str, Any]] = []
    incomplete: list[str] = []
    for raw in draft_cases:
        case_id = str(raw["id"]).strip()
        row = review_by_id[case_id]
        reference_text = row[6]
        terms = [part.strip() for part in row[7].split(",") if part.strip()]
        status = row[8]
        problems: list[str] = []
        if status != REVIEWED_STATUS:
            problems.append(f"status={status or 'blank'}")
        if not reference_text:
            problems.append("verified reference_text is blank")
        if not terms:
            problems.append("verified project_terms are blank")
        if problems:
            incomplete.append(f"{case_id} ({'; '.join(problems)})")
            continue
        cases.append(
            {
                "id": case_id,
                "media_path": str(raw.get("media_path") or "").strip(),
                "language": str(raw.get("language") or "en").strip() or "en",
                "reference_text": reference_text,
                "project_terms": terms,
            }
        )

    if incomplete:
        raise ValueError("human review is incomplete: " + " | ".join(incomplete))

    payload = {"schema_version": 1, "cases": cases}
    validate_manifest_payload(payload)
    return payload


def _sheets_service():
    if not SPREADSHEET_ID:
        raise RuntimeError("SHOCKS_GOOGLE_SPREADSHEET_ID is not configured")
    if not CREDENTIALS_PATH.is_file():
        raise RuntimeError(f"Google service-account credentials not found: {CREDENTIALS_PATH}")
    credentials = service_account.Credentials.from_service_account_file(
        str(CREDENTIALS_PATH),
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
    )
    return build("sheets", "v4", credentials=credentials, cache_discovery=False)


def _validate_media(payload: dict[str, Any]) -> None:
    for case in validate_manifest_payload(payload):
        media = Path(case["media_path"])
        if not media.is_absolute():
            media = (BENCHMARK_DIR / media).resolve()
        if not media.is_file():
            raise FileNotFoundError(f"benchmark media missing for {case['id']}: {media.name}")


def main() -> int:
    if REVIEWED_MANIFEST.is_file():
        return emit(
            {
                "summary": "Reviewed Whisper benchmark manifest already exists; finalizer did not overwrite it",
                "created": False,
                "manifest": "data/whisper_benchmark/manifest.json",
            }
        )
    if not DRAFT_MANIFEST.is_file():
        return emit(
            {
                "summary": "Whisper benchmark review draft is missing; run whisper-benchmark-prepare first",
                "draft_manifest": "data/whisper_benchmark/manifest.draft.json",
            },
            2,
        )

    try:
        draft_payload = json.loads(DRAFT_MANIFEST.read_text(encoding="utf-8"))
        service = _sheets_service()
        response = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range=REVIEW_RANGE,
        ).execute()
        review_rows = list(response.get("values", []))
        payload = build_reviewed_manifest(draft_payload, review_rows)
        _validate_media(payload)
    except Exception as exc:
        return emit(
            {
                "summary": "Whisper benchmark human review is incomplete or invalid",
                "created": False,
                "review_sheet": "Whisper Review",
                "error": str(exc)[:4000],
            },
            2,
        )

    BENCHMARK_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = REVIEWED_MANIFEST.with_suffix(".tmp.json")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp_path.replace(REVIEWED_MANIFEST)
    return emit(
        {
            "summary": "Human-reviewed Whisper benchmark manifest finalized",
            "created": True,
            "case_count": len(payload["cases"]),
            "manifest": "data/whisper_benchmark/manifest.json",
            "review_sheet": "Whisper Review",
        }
    )


if __name__ == "__main__":
    raise SystemExit(main())
