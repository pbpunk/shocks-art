import json
from pathlib import Path
from types import SimpleNamespace

from tools.host_profiles import private_youtube_owner_discovery as discovery
from tools.host_profiles import private_youtube_probe as probe


def test_choose_private_video_id_preserves_order_and_requires_private_processed() -> None:
    ordered_ids = ["new-public", "new-processing-private", "older-private", "oldest-private"]
    detail_items = [
        {"id": "new-public", "status": {"privacyStatus": "public", "uploadStatus": "processed"}},
        {"id": "new-processing-private", "status": {"privacyStatus": "private", "uploadStatus": "uploaded"}},
        {"id": "older-private", "status": {"privacyStatus": "private", "uploadStatus": "processed"}},
        {"id": "oldest-private", "status": {"privacyStatus": "private", "uploadStatus": "processed"}},
    ]

    assert discovery.choose_private_video_id(ordered_ids, detail_items) == "older-private"


def test_choose_private_video_id_returns_blank_without_private_processed_upload() -> None:
    ordered_ids = ["public", "unlisted", "private-processing"]
    detail_items = [
        {"id": "public", "status": {"privacyStatus": "public", "uploadStatus": "processed"}},
        {"id": "unlisted", "status": {"privacyStatus": "unlisted", "uploadStatus": "processed"}},
        {"id": "private-processing", "status": {"privacyStatus": "private", "uploadStatus": "processing"}},
    ]

    assert discovery.choose_private_video_id(ordered_ids, detail_items) == ""


def test_resolve_probe_url_prefers_fixed_host_configuration(monkeypatch) -> None:
    monkeypatch.setenv("SHOCKS_PRIVATE_YOUTUBE_TEST_URL", "https://www.youtube.com/watch?v=fixed123")
    monkeypatch.setattr(probe, "discover_private_owner_video_id", lambda: (_ for _ in ()).throw(AssertionError("discovery should not run")))

    assert probe.resolve_probe_url() == (
        "https://www.youtube.com/watch?v=fixed123",
        "configured-host-url",
        "",
    )


def test_resolve_probe_url_falls_back_to_private_owner_upload(monkeypatch) -> None:
    monkeypatch.delenv("SHOCKS_PRIVATE_YOUTUBE_TEST_URL", raising=False)
    monkeypatch.setattr(probe, "discover_private_owner_video_id", lambda: ("private123", ""))

    assert probe.resolve_probe_url() == (
        "https://www.youtube.com/watch?v=private123",
        "owner-oauth-private-upload",
        "",
    )


def test_resolve_probe_url_preserves_discovery_status(monkeypatch) -> None:
    monkeypatch.delenv("SHOCKS_PRIVATE_YOUTUBE_TEST_URL", raising=False)
    monkeypatch.setattr(probe, "discover_private_owner_video_id", lambda: ("", "owner_oauth_not_connected"))

    assert probe.resolve_probe_url() == ("", "unavailable", "owner_oauth_not_connected")


def test_discovery_subprocess_accepts_only_sanitized_video_id(monkeypatch) -> None:
    payload = {"ok": True, "video_id": "private123"}
    completed = SimpleNamespace(returncode=0, stdout=json.dumps(payload) + "\n", stderr="secret-bearing stderr")
    monkeypatch.setattr(probe.subprocess, "run", lambda *args, **kwargs: completed)

    assert probe.discover_private_owner_video_id() == ("private123", "")


def test_discovery_subprocess_ignores_stderr_and_preserves_safe_status(monkeypatch) -> None:
    payload = {"ok": False, "status": "owner_oauth_not_connected"}
    completed = SimpleNamespace(returncode=2, stdout=json.dumps(payload) + "\n", stderr="secret-bearing stderr")
    monkeypatch.setattr(probe.subprocess, "run", lambda *args, **kwargs: completed)

    assert probe.discover_private_owner_video_id() == ("", "owner_oauth_not_connected")


def test_owner_discovery_helper_uses_bounded_uploads_playlist_not_search() -> None:
    source = (Path(__file__).resolve().parents[1] / "tools" / "host_profiles" / "private_youtube_owner_discovery.py").read_text(encoding="utf-8")

    assert discovery.MAX_OWNER_DISCOVERY_VIDEOS == 200
    assert ".playlistItems()" in source
    assert ".search()" not in source
    assert "configure_live_imports()" in source
    assert "from app.core.database" in source


def test_probe_keeps_live_oauth_imports_out_of_candidate_process() -> None:
    source = (Path(__file__).resolve().parents[1] / "tools" / "host_profiles" / "private_youtube_probe.py").read_text(encoding="utf-8")

    assert "private_youtube_owner_discovery.py" in source
    assert "from app.core.database" not in source
    assert "youtube_analytics" not in source
    assert "googleapiclient" not in source


def test_probe_uses_shared_production_ytdlp_primitives_only() -> None:
    source = (Path(__file__).resolve().parents[1] / "tools" / "host_profiles" / "private_youtube_probe.py").read_text(encoding="utf-8")

    assert "fetch_youtube_metadata" in source
    assert "download_youtube_section" in source
    assert "download_youtube_source" in source
    assert "--cookies-from-browser" not in source
    assert "--download-sections" not in source


def test_safe_ytdlp_failure_never_includes_provider_detail() -> None:
    payload = probe.safe_ytdlp_failure(
        "metadata",
        source_mode="owner-oauth-private-upload",
        video_id="private123",
        elapsed_seconds=1.2345,
    )

    assert payload == {
        "summary": "Private YouTube metadata probe failed",
        "failure_stage": "metadata",
        "source_mode": "owner-oauth-private-upload",
        "error_type": "YtDlpError",
        "credentials_emitted": False,
        "signed_urls_emitted": False,
        "video_id": "private123",
        "metadata_seconds": 1.234,
    }
    assert "error_tail" not in payload
