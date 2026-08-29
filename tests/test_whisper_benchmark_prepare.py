from __future__ import annotations

import pytest

from tools.host_profiles.whisper_benchmark import validate_manifest_payload
from tools.host_profiles.whisper_benchmark_prepare import bounded_window


def test_bounded_review_window_stays_inside_candidate_and_near_evidence() -> None:
    assert bounded_window(100, 200, 130) == (127, 172)
    assert bounded_window(100, 120, 101) == (100, 120)
    assert bounded_window(100, 109, 103) == (100, 109)


def test_review_draft_cannot_be_used_as_benchmark_ground_truth() -> None:
    draft = {
        "review_required": True,
        "cases": [
            {
                "id": "native-ask-example",
                "media_path": "clips/native-ask-example.wav",
                "language": "en",
                "caption_text": "This auto-caption text is only a review seed.",
                "suggested_project_terms": ["Dragon Staff"],
            }
        ],
    }
    with pytest.raises(ValueError, match="reference_text"):
        validate_manifest_payload(draft)
