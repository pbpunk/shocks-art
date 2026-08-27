from __future__ import annotations

from dataclasses import dataclass

from app.host_bridge import profile_policy
from tools.clips_native_ask_smoke_live import (
    PREFERRED_REGRESSION_VIDEO_ID,
    safe_job_receipt,
    select_target_stream,
)
from tools.run_host_profile import PROFILE_SCRIPTS, PROFILE_TIMEOUT_SECONDS


@dataclass
class FakeStream:
    source_video_id: str


def test_smoke_profile_is_fixed_main_only_and_bounded() -> None:
    assert profile_policy("clips-native-ask-smoke") == "main-only"
    assert PROFILE_SCRIPTS["clips-native-ask-smoke"].name == "clips_native_ask_smoke.py"
    assert PROFILE_TIMEOUT_SECONDS["clips-native-ask-smoke"] == 1200


def test_smoke_prefers_reported_regression_video_when_pending() -> None:
    newest = FakeStream("newest-video")
    regression = FakeStream(PREFERRED_REGRESSION_VIDEO_ID)

    assert select_target_stream([newest, regression]) is regression
    assert select_target_stream([newest]) is newest
    assert select_target_stream([]) is None


def test_smoke_receipt_strips_runtime_paths_and_urls() -> None:
    receipt = safe_job_receipt(
        {
            "status": "failed",
            "message": "Ask failed",
            "source": "native-youtube-gemini-sidebar-clips-update",
            "returncode": 1,
            "attempt": "primary",
            "command": "python C:/secret/path.py --url https://youtube.example/private",
            "video_url": "https://youtube.example/private",
            "prompt_path": "C:/secret/prompt.txt",
            "output_path": "C:/secret/output.txt",
        }
    )

    assert receipt == {
        "status": "failed",
        "message": "Ask failed",
        "source": "native-youtube-gemini-sidebar-clips-update",
        "returncode": 1,
        "attempt": "primary",
    }
