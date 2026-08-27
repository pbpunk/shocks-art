from __future__ import annotations

import pytest

from app.host_bridge import (
    HOST_PROFILES,
    HOST_PROFILE_POLICIES,
    HostBridgeValidationError,
    normalize_revision,
    parse_request_row,
    profile_policy,
    request_fingerprint,
    validate_profile,
    validate_request_id,
)


def test_host_profiles_are_fixed_and_narrow() -> None:
    assert HOST_PROFILES == (
        "whisper-benchmark",
        "private-youtube-probe",
        "indexer-soak",
        "clips-native-ask-smoke",
        "clips-native-ask-rerun",
        "derived-data-reinitialize",
        "repo-tests",
    )
    assert set(HOST_PROFILE_POLICIES) == set(HOST_PROFILES)
    assert profile_policy("indexer-soak") == "main-only"
    assert profile_policy("clips-native-ask-smoke") == "main-only"
    assert profile_policy("clips-native-ask-rerun") == "main-only"
    assert profile_policy("derived-data-reinitialize") == "main-only"
    assert profile_policy("whisper-benchmark") == "candidate-or-main"
    assert profile_policy("repo-tests") == "candidate-or-main"


def test_request_id_rejects_shellish_values() -> None:
    for value in ("", "../x", "x y", "x;whoami", "x|cmd", "x\\path", "x/path"):
        with pytest.raises(HostBridgeValidationError):
            validate_request_id(value)


def test_revision_requires_full_sha() -> None:
    sha = "A" * 40
    assert normalize_revision(sha) == sha.lower()
    for value in ("main", "abc123", "0" * 39, "0" * 41, "origin/main"):
        with pytest.raises(HostBridgeValidationError):
            normalize_revision(value)


def test_profile_rejects_arbitrary_commands_and_paths() -> None:
    assert validate_profile("WHISPER-BENCHMARK") == "whisper-benchmark"
    for value in ("python -c print(1)", "../../tools/foo.py", "private-youtube-probe --url https://example.com", "powershell"):
        with pytest.raises(HostBridgeValidationError):
            validate_profile(value)


def test_request_row_uses_only_immutable_request_inputs() -> None:
    request = parse_request_row(["req-001", "2026-08-26T16:00:00Z", "1" * 40, "indexer-soak", "chatgpt", "queued", "ignored-output-cell"])
    assert request.request_id == "req-001"
    assert request.expected_revision == "1" * 40
    assert request.profile == "indexer-soak"
    assert request.requester_id == "chatgpt"
    assert request_fingerprint(request) == ("req-001", "1" * 40, "indexer-soak")
