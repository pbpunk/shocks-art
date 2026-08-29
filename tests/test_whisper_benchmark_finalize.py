from __future__ import annotations

import pytest

from tools.host_profiles.whisper_benchmark_finalize import build_reviewed_manifest


def _draft() -> dict:
    return {
        "cases": [
            {"id": "case-a", "media_path": "clips/a.wav", "language": "en"},
            {"id": "case-b", "media_path": "clips/b.wav", "language": "en"},
        ]
    }


def _row(case_id: str, *, text: str = "spoken words", terms: str = "Dragon Staff", status: str = "Reviewed") -> list[str]:
    return [case_id, "source", "0", "30", "seed", "suggested", text, terms, status, ""]


def test_build_reviewed_manifest_uses_only_verified_fields() -> None:
    payload = build_reviewed_manifest(
        _draft(),
        [
            _row("case-a", text="Exact human checked words.", terms="Dragon Staff, epoxy"),
            _row("case-b", text="Another checked excerpt.", terms="Lichtenberg effect"),
        ],
    )
    assert payload["cases"] == [
        {
            "id": "case-a",
            "media_path": "clips/a.wav",
            "language": "en",
            "reference_text": "Exact human checked words.",
            "project_terms": ["Dragon Staff", "epoxy"],
        },
        {
            "id": "case-b",
            "media_path": "clips/b.wav",
            "language": "en",
            "reference_text": "Another checked excerpt.",
            "project_terms": ["Lichtenberg effect"],
        },
    ]


def test_build_reviewed_manifest_fails_closed_on_unreviewed_row() -> None:
    with pytest.raises(ValueError, match="human review is incomplete"):
        build_reviewed_manifest(_draft(), [_row("case-a"), _row("case-b", status="Needs review")])


def test_build_reviewed_manifest_requires_verified_text_and_terms() -> None:
    with pytest.raises(ValueError, match="verified reference_text is blank"):
        build_reviewed_manifest(_draft(), [_row("case-a", text=""), _row("case-b")])
    with pytest.raises(ValueError, match="verified project_terms are blank"):
        build_reviewed_manifest(_draft(), [_row("case-a", terms=""), _row("case-b")])


def test_build_reviewed_manifest_requires_exact_case_ids() -> None:
    with pytest.raises(ValueError, match="missing review rows"):
        build_reviewed_manifest(_draft(), [_row("case-a")])
    with pytest.raises(ValueError, match="unexpected review rows"):
        build_reviewed_manifest(_draft(), [_row("case-a"), _row("case-b"), _row("case-c")])
