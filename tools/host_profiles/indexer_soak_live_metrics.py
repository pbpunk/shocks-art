from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from tools.host_profiles.indexer_soak_redundancy import collect_long_form_redundancy

CODE_ROOT = Path(__file__).resolve().parents[2]
LIVE_ROOT = Path(os.getenv("SHOCKS_HOST_LIVE_ROOT", CODE_ROOT)).resolve()


def emit(payload: dict[str, Any], code: int = 0) -> int:
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    return code


def configure_live_imports() -> None:
    live = str(LIVE_ROOT)
    candidate = str(CODE_ROOT)
    filtered: list[str] = []
    for entry in sys.path:
        try:
            resolved = str(Path(entry or ".").resolve())
        except OSError:
            resolved = entry
        if resolved == candidate or resolved.startswith(candidate + os.sep):
            continue
        if resolved == live:
            continue
        filtered.append(entry)
    sys.path[:] = [live, *filtered]
    os.chdir(LIVE_ROOT)


def trace_inventory() -> dict[str, Any]:
    from sqlalchemy import func, select

    from app.core.database import SessionLocal
    from app.library_models import Trace

    with SessionLocal() as db:
        rows = list(db.execute(select(Trace.trace_type, func.count()).group_by(Trace.trace_type)).all())
    by_type = {str(trace_type): int(count) for trace_type, count in rows}
    return {
        "total": sum(by_type.values()),
        "by_type": by_type,
        "source": "live-production-sqlite",
    }


def main() -> int:
    model_id = os.getenv("SHOCKS_INDEXER_SOAK_MODEL_ID", "").strip()
    try:
        dimension = int(os.getenv("SHOCKS_INDEXER_SOAK_DIMENSION", "0"))
    except ValueError:
        dimension = 0
    if not model_id or dimension <= 0:
        return emit({"ok": False, "error_type": "ActiveVisualGenerationUnavailable"}, 2)

    try:
        configure_live_imports()
        traces = trace_inventory()
        redundancy = collect_long_form_redundancy(
            live_root=LIVE_ROOT,
            model_id=model_id,
            dimension=dimension,
        )
    except Exception as exc:
        return emit({"ok": False, "error_type": type(exc).__name__}, 1)

    return emit(
        {
            "ok": bool(traces.get("total")) and bool(redundancy.get("available")),
            "trace_volume": traces,
            "long_form_visual_redundancy": redundancy,
        },
        0 if traces.get("total") and redundancy.get("available") else 1,
    )


if __name__ == "__main__":
    raise SystemExit(main())
