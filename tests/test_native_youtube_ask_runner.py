import pytest

from tools.native_youtube_ask_runner import ask_panel_is_open, ensure_ask_panel_open, extract_appended_panel_text


class FakeLocator:
    def __init__(self, visible: bool = False):
        self.visible = visible
        self.clicked = False

    def count(self) -> int:
        return 1 if self.visible else 0

    def nth(self, _index: int):
        return self

    def is_visible(self, timeout: int = 0) -> bool:
        del timeout
        return self.visible

    def click(self, timeout: int = 0) -> None:
        del timeout
        self.clicked = True

    def wait_for(self, timeout: int = 0) -> None:
        del timeout


class AlreadyOpenPage:
    def __init__(self):
        self.heading = FakeLocator(True)
        self.button_lookup_attempted = False

    def get_by_text(self, _pattern):
        return self.heading

    def get_by_role(self, *_args, **_kwargs):
        self.button_lookup_attempted = True
        return FakeLocator(False)

    def locator(self, _selector):
        self.button_lookup_attempted = True
        return FakeLocator(False)


def test_already_open_ask_panel_is_valid_ready_state() -> None:
    page = AlreadyOpenPage()
    assert ask_panel_is_open(page) is True
    ensure_ask_panel_open(page)
    assert page.button_lookup_attempted is False


def test_extract_appended_panel_text_excludes_prior_conversation() -> None:
    initial = "Ask about this video\nOLD ANSWER: Studio Tour and Finished Pieces"
    current = initial + "\nCURRENT ANSWER: Fractal Burning Process and Safety"

    assert extract_appended_panel_text(initial, current) == "CURRENT ANSWER: Fractal Burning Process and Safety"


def test_extract_appended_panel_text_handles_rewritten_volatile_tail() -> None:
    initial = (
        "Ask about this video\n"
        "OLD ANSWER: Studio Tour and Finished Pieces\n"
        "AI can make mistakes\n"
        "Old suggested question"
    )
    current = (
        "Ask about this video\n"
        "OLD ANSWER: Studio Tour and Finished Pieces\n"
        "CURRENT ANSWER: Fractal Burning Process and Safety\n"
        "AI can make mistakes\n"
        "New suggested question"
    )

    assert extract_appended_panel_text(initial, current).startswith(
        "CURRENT ANSWER: Fractal Burning Process and Safety"
    )
    assert "OLD ANSWER" not in extract_appended_panel_text(initial, current)


def test_extract_appended_panel_text_handles_empty_panel() -> None:
    assert extract_appended_panel_text("", "CURRENT ANSWER") == "CURRENT ANSWER"


def test_extract_appended_panel_text_rejects_unrelated_context_change() -> None:
    with pytest.raises(RuntimeError, match="too much"):
        extract_appended_panel_text(
            "Ask about this video\nOLD ANSWER",
            "Ask about a different video\nUNRELATED ANSWER",
        )
