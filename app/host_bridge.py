from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

REQUEST_ID_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
REVISION_RE: Final = re.compile(r"^[0-9a-fA-F]{40}$")

HOST_PROFILES: Final[tuple[str, ...]] = (
    "whisper-benchmark",
    "private-youtube-probe",
    "indexer-soak",
    "clips-native-ask-smoke",
    "clips-native-ask-rerun",
    "derived-data-reinitialize",
    "derived-data-resume",
    "retrieval-quality",
    "repo-tests",
)
HOST_PROFILE_POLICIES: Final[dict[str, str]] = {
    "whisper-benchmark": "candidate-or-main",
    "private-youtube-probe": "candidate-or-main",
    "indexer-soak": "main-only",
    "clips-native-ask-smoke": "main-only",
    "clips-native-ask-rerun": "main-only",
    "derived-data-reinitialize": "main-only",
    "derived-data-resume": "main-only",
    "retrieval-quality": "main-only",
    "repo-tests": "candidate-or-main",
}
VERIFICATION_HEADERS: Final[tuple[str, ...]] = (
    "request_id", "created_at", "expected_revision", "profile", "requester_id", "state",
    "started_at", "finished_at", "tested_revision", "outcome", "exit_code",
    "duration_seconds", "summary", "result_json",
)
STATE_HEADERS: Final[tuple[str, ...]] = (
    "host_updated_at", "host_status", "host_revision", "host_last_request_id",
    "host_last_error", "host_active_request_id",
)
TERMINAL_STATES: Final[frozenset[str]] = frozenset({"completed", "failed", "superseded", "rejected"})


class HostBridgeValidationError(ValueError):
    pass


@dataclass(frozen=True)
class HostVerificationRequest:
    request_id: str
    created_at: str
    expected_revision: str
    profile: str
    requester_id: str = ""


def validate_request_id(value: object) -> str:
    request_id = str(value or "").strip()
    if not REQUEST_ID_RE.fullmatch(request_id):
        raise HostBridgeValidationError("request_id must be 1-80 characters using only letters, digits, '.', '_' or '-'")
    return request_id


def normalize_revision(value: object) -> str:
    revision = str(value or "").strip().lower()
    if not REVISION_RE.fullmatch(revision):
        raise HostBridgeValidationError("expected_revision must be a full 40-character Git commit SHA")
    return revision


def validate_profile(value: object) -> str:
    profile = str(value or "").strip().lower()
    if profile not in HOST_PROFILES:
        raise HostBridgeValidationError(f"profile must be one of: {', '.join(HOST_PROFILES)}")
    return profile


def profile_policy(profile: str) -> str:
    return HOST_PROFILE_POLICIES[validate_profile(profile)]


def parse_request_row(row: list[object] | tuple[object, ...]) -> HostVerificationRequest:
    padded = [str(value or "") for value in row]
    padded.extend([""] * max(0, len(VERIFICATION_HEADERS) - len(padded)))
    return HostVerificationRequest(
        request_id=validate_request_id(padded[0]),
        created_at=padded[1].strip(),
        expected_revision=normalize_revision(padded[2]),
        profile=validate_profile(padded[3]),
        requester_id=padded[4].strip(),
    )


def request_fingerprint(request: HostVerificationRequest) -> tuple[str, str, str]:
    return request.request_id, request.expected_revision, request.profile
