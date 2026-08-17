import json
from pathlib import Path


STATE_PATH = Path("data/clip_new_state.json")


def read_new_clip_ids() -> set[str]:
    if not STATE_PATH.exists():
        return set()
    try:
        payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return set()
    ids = payload.get("candidate_window_ids", [])
    return {str(candidate_id) for candidate_id in ids if candidate_id}


def replace_new_clip_ids(candidate_ids: set[str]) -> None:
    """Replace the NEW batch only when callers have actually created clips."""
    if not candidate_ids:
        return
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = STATE_PATH.with_suffix(".tmp")
    temp_path.write_text(
        json.dumps({"candidate_window_ids": sorted(candidate_ids)}, indent=2),
        encoding="utf-8",
    )
    temp_path.replace(STATE_PATH)
