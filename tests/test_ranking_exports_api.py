import json

from app.services.export import export_csv, export_json
from app.services.ranking import weighted_score
from app.services.repository import create_analysis_run, save_candidate, upsert_stream
from app.services.validation import validate_candidate_response


def test_weighted_ranking_score():
    score = weighted_score({"hook_strength": 100, "editing_potential": 100})
    assert score > 20


def create_candidate(db_session, payload, title="Stream"):
    stream_data = {
        "platform": "youtube",
        "channel_id": "channel_1",
        "source_video_id": payload["source_video_id"],
        "title": title,
        "description": "",
        "url": f"https://www.youtube.com/watch?v={payload['source_video_id']}",
        "published_at": "2026-07-31T12:00:00Z",
        "duration": 1200,
        "thumbnail": "",
        "processing_status": "queued",
        "schema_version": "1.0",
    }
    stream, _ = upsert_stream(db_session, stream_data)
    payload["stream_id"] = stream.stream_id
    run = create_analysis_run(db_session, stream, "fake", "1.0", "1.0")
    candidate = save_candidate(db_session, run, validate_candidate_response(payload))
    db_session.commit()
    return candidate


def test_pillar_filter_and_exports(db_session, valid_candidate_data):
    candidate = create_candidate(db_session, valid_candidate_data)
    assert "mistake-recovery" in candidate.tags
    csv_output = export_csv([candidate])
    json_output = json.loads(export_json([candidate]))
    assert "mistakes_problem_solving" in csv_output
    assert json_output[0]["candidate_window_id"] == candidate.candidate_window_id


def test_api_dashboard_and_fixture(client):
    response = client.post("/api/fixtures/load")
    assert response.status_code == 200
    assert response.json()["stream_id"]
    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert 'data-app-tab="clips"' in dashboard.text
    assert 'href="/library"' in dashboard.text
    assert 'href="/debug"' in dashboard.text
    pipeline = client.get("/pipeline")
    assert pipeline.status_code == 200
    assert "Livestream Pipeline" in pipeline.text
    native_ask = client.get("/native-ask", follow_redirects=False)
    assert native_ask.status_code == 303
    debug = client.get("/debug")
    assert debug.status_code == 200
    assert "gemini-audit-console" in debug.text


def test_prefixed_app_path_renders_internal_links(client):
    response = client.get("/shocks_art/")

    assert response.status_code == 200
    assert 'href="/shocks_art/static/styles.css?v=analytics-mobile-1"' in response.text
    assert 'href="/shocks_art/"' in response.text
    assert 'href="/shocks_art/library"' in response.text
    assert 'href="/shocks_art/debug"' in response.text


def test_tailscale_forwarded_host_renders_prefixed_links(client):
    response = client.get("/", headers={"host": "desktop.tail27cee7.ts.net"})

    assert response.status_code == 200
    assert 'href="/shocks_art/static/styles.css?v=analytics-mobile-1"' in response.text
    assert 'href="/shocks_art/"' in response.text
    assert 'href="/shocks_art/library"' in response.text
    assert 'href="/shocks_art/debug"' in response.text


def test_prefixed_actions_redirect_with_prefix(client, monkeypatch):
    def failing_discovery(db):
        raise RuntimeError("YouTube API unavailable")

    monkeypatch.setattr("app.main.discover_and_store_streams", failing_discovery)

    response = client.post("/shocks_art/actions/discover", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/shocks_art/pipeline?archive_status=error")


def test_clips_home_includes_non_structured_candidates(client, db_session, valid_candidate_data):
    candidate = create_candidate(db_session, valid_candidate_data, title="Newest Gemini Stream")

    response = client.get("/")

    assert response.status_code == 200
    assert candidate.title in response.text
    assert "Newest Gemini Stream" in response.text
