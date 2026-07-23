from __future__ import annotations


def test_frontend_and_static_assets_are_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "<title>axiom</title>" in response.text
    assert "/static/js/state.js?v=20260723.3" in response.text
    assert "/static/js/v2-adapter.js?v=20260723.3" in response.text

    asset = client.get("/static/js/v2-adapter.js")
    assert asset.status_code == 200
    assert "Backend V2 adapter" in asset.text

    state = client.get("/static/js/state.js")
    assert state.status_code == 200
    assert "projectDetailsLoaded: false" in state.text
    assert "projectsLoaded: false" in state.text

    dashboard = client.get("/static/templates/dashboard.html")
    assert dashboard.status_code == 200
    assert "正在加载任务" in dashboard.text
    assert "正在加载事件" in dashboard.text


def test_frontend_fallback_does_not_mask_unknown_api_routes(client):
    assert client.get("/tasks/t_123").status_code == 200

    response = client.get("/api/v1/does-not-exist")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
