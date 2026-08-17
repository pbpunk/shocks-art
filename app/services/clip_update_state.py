import json
from datetime import datetime, timezone
from pathlib import Path


STATE_PATH = Path("data/clip_update_state.json")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_clip_update_state() -> dict:
    if not STATE_PATH.exists():
        return {"status": "idle"}
    try:
        payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {"status": "idle"}
    return payload if isinstance(payload, dict) else {"status": "idle"}


def write_clip_update_state(status: str, message: str = "", **extra) -> dict:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    previous = read_clip_update_state()
    started_at = previous.get("started_at") if status in {"processing", "complete", "failed"} else None
    if status == "checking" or not started_at:
        started_at = _now()
    payload = {
        "status": status,
        "message": message,
        "started_at": started_at,
        "updated_at": _now(),
        **extra,
    }
    temp_path = STATE_PATH.with_suffix(".tmp")
    temp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temp_path.replace(STATE_PATH)
    return payload
