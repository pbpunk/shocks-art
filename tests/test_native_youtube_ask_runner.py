import pytest

from tools.native_youtube_ask_runner import extract_appended_panel_text


def test_extract_appended_panel_text_excludes_prior_conversation() -> None:
    initial = "Ask about this video\nOLD ANSWER: Studio Tour and Finished Pieces"
    current = initial + "\nCURRENT ANSWER: Fractal Burning Process and Safety"

    assert extract_appended_panel_text(initial, current) == "CURRENT ANSWER: Fractal Burning Process and Safety"


def test_extract_appended_panel_text_handles_empty_panel() -> None:
    assert extract_appended_panel_text("", "CURRENT ANSWER") == "CURRENT ANSWER"


def test_extract_appended_panel_text_rejects_non_append_only_context_change() -> None:
    with pytest.raises(RuntimeError, match="non-append-only"):
        extract_appended_panel_text(
            "Ask about this video\nOLD ANSWER",
            "Ask about a different video\nUNRELATED ANSWER",
        )
