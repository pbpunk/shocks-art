from __future__ import annotations

from pathlib import Path

import pytest

from tools.google_update_worker import UPDATES_HEADERS, normalize_revision, validate_request_id


ROOT = Path(__file__).resolve().parents[1]


def test_update_sheet_has_only_four_requester_inputs() -> None:
    assert UPDATES_HEADERS[:4] == (
        "request_id",
        "created_at",
        "expected_revision",
        "requester_id",
    )
    assert UPDATES_HEADERS[4:] == (
        "state",
        "launched_at",
        "finished_at",
        "running_revision",
        "outcome",
        "error",
    )


def test_update_revision_is_exact_sha_only() -> None:
    sha = "A" * 40
    assert normalize_revision(sha) == sha.lower()
    for value in ("main", "origin/main", "abc123", "0" * 39, "0" * 41, "HEAD"):
        with pytest.raises(ValueError):
            normalize_revision(value)


def test_update_request_id_rejects_shellish_values() -> None:
    assert validate_request_id("chatgpt-update-001") == "chatgpt-update-001"
    for value in ("", "../x", "x y", "x;whoami", "x|cmd", "x/path", "x\\path"):
        with pytest.raises(ValueError):
            validate_request_id(value)


def test_update_helper_invokes_only_canonical_updater() -> None:
    helper = (ROOT / "tools" / "google_update_helper.py").read_text(encoding="utf-8")
    assert 'UPDATE_CMD = ROOT / "Update App.cmd"' in helper
    assert "shell=True" not in helper
    assert '"cmd.exe", "/d", "/c", str(UPDATE_CMD)' in helper


def test_worker_uses_fixed_helper_and_no_sheet_command_field() -> None:
    worker = (ROOT / "tools" / "google_update_worker.py").read_text(encoding="utf-8")
    assert 'HELPER_PATH = ROOT / "tools" / "google_update_helper.py"' in worker
    assert "shell=True" not in worker
    assert "command" not in {header.lower() for header in UPDATES_HEADERS}
    assert "path" not in {header.lower() for header in UPDATES_HEADERS}
    assert "branch" not in {header.lower() for header in UPDATES_HEADERS}
