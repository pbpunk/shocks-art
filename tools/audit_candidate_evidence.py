from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from app.core.database import SessionLocal, init_db  # noqa: E402
from app.services.candidate_evidence import audit_candidate_windows  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only audit of CandidateWindow transcript evidence against stored timestamped captions."
    )
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--candidate-id", default=None)
    parser.add_argument(
        "--all-models",
        action="store_true",
        help="Audit candidates from every analysis model instead of direct gemini-* runs only.",
    )
    parser.add_argument(
        "--fail-only",
        action="store_true",
        help="Print only failed/unverifiable candidates in the items array.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    init_db()
    with SessionLocal() as db:
        audits = audit_candidate_windows(
            db,
            limit=args.limit,
            direct_gemini_only=not args.all_models,
            candidate_id=args.candidate_id,
        )

    counts = {"pass": 0, "fail": 0, "unverifiable": 0}
    for audit in audits:
        counts[audit.status] = counts.get(audit.status, 0) + 1

    items = [audit.as_dict() for audit in audits]
    if args.fail_only:
        items = [item for item in items if item["status"] != "pass"]

    print(
        json.dumps(
            {
                "ok": True,
                "readOnly": True,
                "directGeminiOnly": not args.all_models,
                "considered": len(audits),
                "counts": counts,
                "items": items,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 1 if counts.get("fail", 0) else 0


if __name__ == "__main__":
    raise SystemExit(main())
