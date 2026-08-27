from __future__ import annotations

import json
from typing import Any

from sqlalchemy import delete, func, select

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models import DerivedAsset, PerformanceRecord, PublishingRecord
from tools.reinitialize_derived_data_live import backup_database, sqlite_database_path


def emit(payload: dict[str, Any], code: int = 0) -> int:
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    return code


def published_lineage_counts(*, publishing_records: int, performance_records: int) -> dict[str, int]:
    counts = {
        "publishingRecords": int(publishing_records),
        "performanceRecords": int(performance_records),
    }
    return {key: value for key, value in counts.items() if value > 0}


def main() -> int:
    try:
        settings = get_settings()
        database_path = sqlite_database_path(settings.database_url)
        with SessionLocal() as db:
            derived_assets = int(db.scalar(select(func.count()).select_from(DerivedAsset)) or 0)
            publishing_records = int(db.scalar(select(func.count()).select_from(PublishingRecord)) or 0)
            performance_records = int(db.scalar(select(func.count()).select_from(PerformanceRecord)) or 0)
            protected = published_lineage_counts(
                publishing_records=publishing_records,
                performance_records=performance_records,
            )
            if protected:
                return emit(
                    {
                        "summary": "Refusing reinitialization because published/performance lineage exists",
                        "protected_counts": protected,
                        "derived_assets": derived_assets,
                    },
                    3,
                )
            if derived_assets == 0:
                return emit(
                    {
                        "summary": "No unpublished DerivedAsset lineage required pre-clear",
                        "derived_assets_removed": 0,
                        "preclear_backup_file": "",
                    }
                )

        # The first safety backup preserves the exact pre-reset database, including
        # unpublished DerivedAsset rows. External/editor output files are untouched.
        backup_name = backup_database(database_path)
        with SessionLocal() as db:
            db.execute(delete(DerivedAsset))
            db.commit()
            remaining = int(db.scalar(select(func.count()).select_from(DerivedAsset)) or 0)
        if remaining:
            return emit(
                {
                    "summary": f"Failed to clear {remaining} unpublished DerivedAsset row(s)",
                    "derived_assets_removed": derived_assets - remaining,
                    "preclear_backup_file": backup_name,
                },
                1,
            )
        return emit(
            {
                "summary": f"Cleared {derived_assets} unpublished DerivedAsset row(s) after safety backup",
                "derived_assets_removed": derived_assets,
                "preclear_backup_file": backup_name,
            }
        )
    except Exception as exc:
        return emit({"summary": f"Derived-data pre-clear failed: {type(exc).__name__}: {exc}"}, 1)


if __name__ == "__main__":
    raise SystemExit(main())
