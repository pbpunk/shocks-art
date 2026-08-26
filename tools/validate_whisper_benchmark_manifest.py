from __future__ import annotations

import json
import sys
from pathlib import Path

from tools.host_profiles.whisper_benchmark import load_manifest, resolve_manifest_path


def main() -> int:
    path = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else resolve_manifest_path()
    if not path.is_file():
        print(json.dumps({"ok": False, "manifest": str(path), "error": "manifest file does not exist"}))
        return 2
    try:
        cases = load_manifest(path)
    except Exception as exc:
        print(json.dumps({"ok": False, "manifest": str(path), "error": str(exc)}))
        return 2
    print(
        json.dumps(
            {
                "ok": True,
                "manifest": str(path),
                "caseCount": len(cases),
                "projectTermCount": sum(len(case["project_terms"]) for case in cases),
                "caseIds": [case["id"] for case in cases],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
