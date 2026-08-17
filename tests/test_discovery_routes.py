def test_discover_action_redirects_with_error_message(client, monkeypatch):
    def failing_discovery(db):
        raise RuntimeError("YouTube API unavailable")

    monkeypatch.setattr("app.main.discover_and_store_streams", failing_discovery)

    response = client.post("/actions/discover", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/pipeline?archive_status=error")
    assert "YouTube+API+unavailable" in response.headers["location"]


def test_discover_api_returns_controlled_error(client, monkeypatch):
    def failing_discovery(db):
        raise RuntimeError("YouTube API unavailable")

    monkeypatch.setattr("app.main.discover_and_store_streams", failing_discovery)

    response = client.post("/api/discover")

    assert response.status_code == 503
    assert response.json() == {"detail": "YouTube API unavailable"}


def test_pipeline_renders_archive_refresh_message(client):
    response = client.get(
        "/pipeline",
        params={
            "archive_status": "success",
            "archive_message": "Archive refreshed: 1 new, 0 updated, 1 transcripts available, 0 missing.",
        },
    )

    assert response.status_code == 200
    assert "Archive refresh complete." in response.text
    assert "Archive refreshed: 1 new, 0 updated" in response.text
