def test_health_contract_is_available_locally_and_through_prefix(client):
    local = client.get("/health")
    prefixed = client.get("/shocks_art/health")

    assert local.status_code == 200
    assert local.json() == {"status": "ok", "app": "shocks-art"}
    assert prefixed.status_code == 200
    assert prefixed.json() == local.json()


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
