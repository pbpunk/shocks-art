from pathlib import Path

from tools.host_profiles import private_youtube_probe as probe


def test_choose_private_video_id_preserves_search_order_and_requires_private_processed() -> None:
    search_items = [
        {"id": {"videoId": "new-public"}},
        {"id": {"videoId": "new-processing-private"}},
        {"id": {"videoId": "older-private"}},
        {"id": {"videoId": "oldest-private"}},
    ]
    detail_items = [
        {"id": "new-public", "status": {"privacyStatus": "public", "uploadStatus": "processed"}},
        {"id": "new-processing-private", "status": {"privacyStatus": "private", "uploadStatus": "uploaded"}},
        {"id": "older-private", "status": {"privacyStatus": "private", "uploadStatus": "processed"}},
        {"id": "oldest-private", "status": {"privacyStatus": "private", "uploadStatus": "processed"}},
    ]

    assert probe.choose_private_video_id(search_items, detail_items) == "older-private"


def test_choose_private_video_id_returns_blank_without_private_processed_upload() -> None:
    search_items = [
        {"id": {"videoId": "public"}},
        {"id": {"videoId": "unlisted"}},
        {"id": {"videoId": "private-processing"}},
    ]
    detail_items = [
        {"id": "public", "status": {"privacyStatus": "public", "uploadStatus": "processed"}},
        {"id": "unlisted", "status": {"privacyStatus": "unlisted", "uploadStatus": "processed"}},
        {"id": "private-processing", "status": {"privacyStatus": "private", "uploadStatus": "processing"}},
    ]

    assert probe.choose_private_video_id(search_items, detail_items) == ""


def test_resolve_probe_url_prefers_fixed_host_configuration(monkeypatch) -> None:
    monkeypatch.setenv("SHOCKS_PRIVATE_YOUTUBE_TEST_URL", "https://www.youtube.com/watch?v=fixed123")
    monkeypatch.setattr(probe, "discover_private_owner_url", lambda: (_ for _ in ()).throw(AssertionError("discovery should not run")))

    assert probe.resolve_probe_url() == (
        "https://www.youtube.com/watch?v=fixed123",
        "configured-host-url",
    )


def test_resolve_probe_url_falls_back_to_private_owner_upload(monkeypatch) -> None:
    monkeypatch.delenv("SHOCKS_PRIVATE_YOUTUBE_TEST_URL", raising=False)
    monkeypatch.setattr(
        probe,
        "discover_private_owner_url",
        lambda: "https://www.youtube.com/watch?v=private123",
    )

    assert probe.resolve_probe_url() == (
        "https://www.youtube.com/watch?v=private123",
        "owner-oauth-private-upload",
    )


def test_resolve_probe_url_fails_closed_when_discovery_is_unavailable(monkeypatch) -> None:
    monkeypatch.delenv("SHOCKS_PRIVATE_YOUTUBE_TEST_URL", raising=False)
    monkeypatch.setattr(probe, "discover_private_owner_url", lambda: "")

    assert probe.resolve_probe_url() == ("", "unavailable")


def test_owner_discovery_uses_bounded_uploads_playlist_not_search() -> None:
    source = (Path(__file__).resolve().parents[1] / "tools" / "host_profiles" / "private_youtube_probe.py").read_text(encoding="utf-8")

    assert probe.MAX_OWNER_DISCOVERY_VIDEOS == 200
    assert ".playlistItems()" in source
    assert ".search()" not in source
