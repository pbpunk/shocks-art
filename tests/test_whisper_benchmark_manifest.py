from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.host_profiles import whisper_benchmark


def test_validate_manifest_requires_labeled_project_terms() -> None:
    payload = {
        "cases": [
            {
                "id": "dragon-staff",
                "media_path": "dragon.wav",
                "reference_text": "This is the Dragon Staff.",
                "project_terms": ["Dragon Staff"],
            }
        ]
    }
    cases = whisper_benchmark.validate_manifest_payload(payload)
    assert cases == [
        {
            "id": "dragon-staff",
            "media_path": "dragon.wav",
            "reference_text": "This is the Dragon Staff.",
            "project_terms": ["Dragon Staff"],
            "language": "en",
        }
    ]

    payload["cases"][0]["project_terms"] = []
    with pytest.raises(ValueError, match="at least one project_term"):
        whisper_benchmark.validate_manifest_payload(payload)


def test_validate_manifest_rejects_duplicate_ids() -> None:
    case = {
        "id": "same",
        "media_path": "sample.wav",
        "reference_text": "sample words",
        "project_terms": ["sample"],
    }
    with pytest.raises(ValueError, match="duplicate case id"):
        whisper_benchmark.validate_manifest_payload({"cases": [case, dict(case)]})


def test_load_manifest_resolves_media_relative_to_manifest(tmp_path: Path) -> None:
    clips = tmp_path / "clips"
    clips.mkdir()
    media = clips / "sample.wav"
    media.write_bytes(b"not-real-audio")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "id": "sample",
                        "media_path": "clips/sample.wav",
                        "reference_text": "sample words",
                        "project_terms": ["sample"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    cases = whisper_benchmark.load_manifest(manifest)
    assert cases[0]["media_path"] == str(media.resolve())


def test_missing_manifest_receipt_is_actionable_and_path_sanitized(monkeypatch, tmp_path: Path, capsys) -> None:
    missing = tmp_path / "private" / "manifest.json"
    monkeypatch.setenv("SHOCKS_WHISPER_BENCHMARK_MANIFEST", str(missing))

    assert whisper_benchmark.main() == 2
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["configured"] is False
    assert receipt["expected_manifest"] == "data/whisper_benchmark/manifest.json"
    assert receipt["setup_doc"] == "docs/WHISPER_BENCHMARK.md"
    assert str(tmp_path) not in json.dumps(receipt)
