import json
from pathlib import Path


def assert_health_payload(payload):
    assert payload["status"] == "ok"
    assert payload["ok"] is True
    assert payload["app"] == "shocks-art"
    assert payload["name"] == "Shocks Art"
    assert payload["route"] == "/shocks_art"
    assert payload["version"]
    assert payload["startedAt"]
    assert payload["mode"]
    assert payload["checkedAt"]


def test_health_contract_is_available_locally_and_through_prefix(client):
    local = client.get("/health")
    prefixed = client.get("/shocks_art/health")

    assert local.status_code == 200
    assert prefixed.status_code == 200
    assert_health_payload(local.json())
    assert_health_payload(prefixed.json())
    assert prefixed.json()["startedAt"] == local.json()["startedAt"]


def test_api_ping_is_available_locally_and_through_prefix(client):
    local = client.get("/api/ping")
    prefixed = client.get("/shocks_art/api/ping")

    assert local.status_code == 200
    assert prefixed.status_code == 200
    for payload in (local.json(), prefixed.json()):
        assert payload["ok"] is True
        assert payload["app"] == "shocks-art"
        assert payload["route"] == "/shocks_art"
        assert payload["version"]
    assert prefixed.json() == local.json()


def test_jarvis_manifest_declares_owned_runtime_contract():
    manifest = json.loads(Path("jarvis.app.json").read_text(encoding="utf-8"))

    assert manifest["schemaVersion"] == 1
    assert manifest["id"] == "shocks-art"
    assert manifest["route"] == "/shocks_art"
    assert manifest["ports"] == [8000]
    assert manifest["primaryPort"] == 8000
    assert manifest["health"]["url"] == "http://127.0.0.1:8000/shocks_art/health"
    assert manifest["network"]["upstream"] == "http://127.0.0.1:8000/shocks_art"
    assert manifest["runtime"] == {"file": "data/runtime.json", "logsDir": "data/logs"}
    for command in manifest["lifecycle"].values():
        assert Path(command).is_file(), command


def test_primary_routes_render_through_shocks_art_prefix(client):
    for path in ("/shocks_art/", "/shocks_art/library", "/shocks_art/debug"):
        response = client.get(path)
        assert response.status_code == 200, path


def test_primary_shell_uses_rail_and_mobile_bottom_navigation(client):
    response = client.get("/shocks_art/")

    assert response.status_code == 200
    assert 'class="side-rail"' in response.text
    assert 'class="bottom-nav"' in response.text
    assert response.text.count('data-app-tab="clips"') == 2
    assert response.text.count('data-app-tab="library"') == 2
    assert response.text.count('data-app-tab="debug"') == 2
    assert 'href="/shocks_art/library"' in response.text
    assert 'href="/shocks_art/debug"' in response.text


def test_shell_assets_are_subpath_safe(client):
    shell_css = client.get("/shocks_art/static/app_shell.css")
    swipe_js = client.get("/shocks_art/static/tab_swipe.js")

    assert shell_css.status_code == 200
    assert "#54c6a2" in shell_css.text
    assert ".side-rail" in shell_css.text
    assert ".bottom-nav" in shell_css.text
    assert ".secondary-nav" in shell_css.text

    assert swipe_js.status_code == 200
    assert "edgeGuard = 24" in swipe_js.text
    assert "interactiveSelector" in swipe_js.text
    assert "primaryOrder" in swipe_js.text
