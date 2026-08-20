from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from app.core.database import SessionLocal, init_db  # noqa: E402
from app.services.reanalysis import (  # noqa: E402
    ReanalysisBlockedError,
    build_reanalysis_plan,
    reanalyze_stream,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Safely replace one stream's untouched candidate generation. Dry-run is the default; "
            "--apply runs a fresh analysis first and archives the captured old candidates only after success."
        )
    )
    parser.add_argument("stream_id")
    parser.add_argument(
        "--expected-candidate-id",
        action="append",
        default=[],
        help="Expected active candidate ID. Repeat once for every currently active candidate.",
    )
    parser.add_argument("--reason", default="")
    parser.add_argument("--apply", action="store_true")
    return parser


def _print(payload: dict) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.apply and not args.expected_candidate_id:
        raise SystemExit("--apply requires at least one --expected-candidate-id")
    if args.apply and not args.reason.strip():
        raise SystemExit("--apply requires --reason")

    init_db()
    with SessionLocal() as db:
        try:
            plan = build_reanalysis_plan(
                db,
                args.stream_id,
                expected_candidate_ids=args.expected_candidate_id or None,
            )
            if not args.apply:
                _print({"ok": True, "mode": "dry-run", "plan": plan.as_dict()})
                return 0 if plan.safe else 1

            if not plan.safe:
                _print({"ok": False, "mode": "apply", "plan": plan.as_dict()})
                return 1

            result = reanalyze_stream(
                db,
                args.stream_id,
                expected_candidate_ids=args.expected_candidate_id,
                reason=args.reason.strip(),
            )
            _print({"ok": True, "mode": "apply", "result": result.as_dict()})
            return 0
        except ReanalysisBlockedError as exc:
            _print({"ok": False, "mode": "apply" if args.apply else "dry-run", "blockers": exc.blockers})
            return 1
        except Exception as exc:
            _print(
                {
                    "ok": False,
                    "mode": "apply" if args.apply else "dry-run",
                    "error": str(exc),
                    "errorType": exc.__class__.__name__,
                }
            )
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
