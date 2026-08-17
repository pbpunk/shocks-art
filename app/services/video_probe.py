from datetime import datetime, timezone
from pathlib import Path

from app.core.config import ROOT_DIR, get_settings
from app.models import Stream


PROBE_PROMPT = """Video access probe. Inspect the supplied YouTube video directly.

Focus only on timestamp 00:36:20 and the surrounding 20 seconds.

Return JSON with:
- can_inspect_video: boolean
- can_verify_exact_timestamp: boolean
- visible_scene: string
- audible_speech: string
- uncertainty: string

Do not infer from the title, description, transcript, prior context, or metadata. If you cannot inspect or hear that timestamp, say so.
"""


def probe_path(stream_id: str) -> Path:
    path = ROOT_DIR / "data" / "video_probes"
    path.mkdir(parents=True, exist_ok=True)
    return path / f"{stream_id}.log"


def read_probe(stream_id: str) -> str:
    path = probe_path(stream_id)
    return path.read_text(encoding="utf-8") if path.exists() else ""


def run_video_access_probe(stream: Stream) -> str:
    from google import genai

    settings = get_settings()
    client = genai.Client(api_key=settings.gemini_api_key)
    response = client.interactions.create(
        model=settings.gemini_model,
        input=[
            {"type": "video", "uri": stream.url},
            {"type": "text", "text": PROBE_PROMPT},
        ],
    )
    text = getattr(response, "output_text", None) or getattr(response, "text", None) or ""
    log = (
        f"===== {datetime.now(timezone.utc).isoformat(timespec='seconds')} VIDEO ACCESS PROBE =====\n"
        f"model: {settings.gemini_model}\n"
        f"stream: {stream.title}\n"
        f"url: {stream.url}\n\n"
        f"PROMPT\n{PROBE_PROMPT}\n\n"
        f"RESPONSE\n{text}\n"
    )
    probe_path(stream.stream_id).write_text(log, encoding="utf-8")
    return log
