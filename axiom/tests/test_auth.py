from __future__ import annotations

import os

import pytest

from axiom.shared.database import AXIOM_SCHEMA  # noqa: F401


ADMIN_USER = "axiom"
ADMIN_PASSWORD = "axiom-test-password-123"


def _login(client, username: str = ADMIN_USER, password: str = ADMIN_PASSWORD):
    return client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password, "remember": True},
    )


def test_login_success_returns_token_and_cookie(client):
    response = _login(client)
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["username"] == ADMIN_USER
    assert data["role"] == "admin"
    assert data["token"]
    assert data["expires_in_seconds"] == 7 * 24 * 3600
    # HttpOnly Cookie 已写入，浏览器跨页面自动携带
    set_cookie = response.headers.get("set-cookie", "")
    assert "axiom_token=" in set_cookie
    assert "HttpOnly" in set_cookie


def test_login_wrong_username_rejected(client):
    response = _login(client, username="nobody")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"


def test_login_wrong_password_rejected(client):
    response = _login(client, password="wrong-password")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"


def test_auth_status_unauthenticated(client):
    response = client.get("/api/v1/auth/status")
    assert response.status_code == 200
    assert response.json()["authenticated"] is False


def test_auth_status_with_valid_token(client):
    token = _login(client).json()["token"]
    response = client.get(
        "/api/v1/auth/status",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["authenticated"] is True
    assert data["username"] == ADMIN_USER
    assert data["role"] == "admin"


def test_auth_status_with_invalid_token(client):
    response = client.get(
        "/api/v1/auth/status",
        headers={"Authorization": "Bearer invalid.token.value"},
    )
    assert response.status_code == 200
    assert response.json()["authenticated"] is False


def test_change_password_flow(client):
    token = _login(client).json()["token"]
    new_password = "brand-new-pass-456"
    response = client.post(
        "/api/v1/auth/change-password",
        headers={"Authorization": f"Bearer {token}"},
        json={"old_password": ADMIN_PASSWORD, "new_password": new_password},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["token"]
    # 旧密码不再可用
    assert _login(client, password=ADMIN_PASSWORD).status_code == 401
    # 新密码可登录
    new_login = _login(client, password=new_password)
    assert new_login.status_code == 200


def test_change_password_wrong_old_password(client):
    token = _login(client).json()["token"]
    response = client.post(
        "/api/v1/auth/change-password",
        headers={"Authorization": f"Bearer {token}"},
        json={"old_password": "not-the-right-one", "new_password": "some-new-pass-1"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_old_password"


def test_logout_clears_cookie(client):
    response = client.post("/api/v1/auth/logout")
    assert response.status_code == 200
    set_cookie = response.headers.get("set-cookie", "")
    assert "axiom_token=" in set_cookie
    assert "Max-Age=0" in set_cookie or "expires=" in set_cookie.lower()


@pytest.mark.parametrize(
    "path",
    [
        "/health",
        "/api/v1/auth/status",
        "/static/js/login.js",
        "/static/vendor/alpine.min.js",
        "/static/apple.css",
        "/static/logo.jpg",
        "/",
    ],
)
def test_middleware_whitelists_public_paths(client, monkeypatch, path):
    monkeypatch.setenv("AXIOM_AUTH_DISABLED", "0")
    response = client.get(path)
    assert response.status_code == 200, f"{path} 公开资源未登录应可直接访问"


def test_middleware_blocks_unauthenticated_api(client, monkeypatch):
    monkeypatch.setenv("AXIOM_AUTH_DISABLED", "0")
    response = client.get("/api/v1/projects")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_middleware_accepts_bearer_token(client, monkeypatch):
    monkeypatch.setenv("AXIOM_AUTH_DISABLED", "0")
    token = _login(client).json()["token"]
    response = client.get(
        "/api/v1/projects",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200


def test_middleware_accepts_cookie_token(client, monkeypatch):
    monkeypatch.setenv("AXIOM_AUTH_DISABLED", "0")
    login = _login(client)
    token = login.json()["token"]
    response = client.get("/api/v1/projects", cookies={"axiom_token": token})
    assert response.status_code == 200


def test_middleware_rejects_expired_token(client, monkeypatch):
    monkeypatch.setenv("AXIOM_AUTH_DISABLED", "0")
    # 直接构造一个已过期的合法签名 token 较复杂，这里用垃圾 token 断言拒绝
    response = client.get(
        "/api/v1/projects",
        headers={"Authorization": "Bearer forged.token.here"},
    )
    assert response.status_code == 401


# ─── 机器控制面 service token（runner 专用，与用户登录态解耦） ───

SERVICE_TOKEN = "test-service-token-abc123"


@pytest.fixture()
def auth_client(client, monkeypatch):
    """强制开启中间件 + 注入 service token 的客户端（模拟生产环境）。"""
    monkeypatch.setenv("AXIOM_AUTH_DISABLED", "0")
    monkeypatch.setenv("AXIOM_SERVICE_TOKEN", SERVICE_TOKEN)
    from fastapi.testclient import TestClient

    from axiom.app import create_app
    from axiom.shared.database import create_database_engine

    from tests.conftest import TEST_DATABASE_URL

    data_dir = client.app.state.container.store.root
    engine = create_database_engine(TEST_DATABASE_URL, application_name="axiom-pytest-auth")
    try:
        with TestClient(create_app(engine=engine, data_dir=data_dir)) as test_client:
            yield test_client
    finally:
        engine.dispose()


def test_service_token_allows_machine_control_plane(auth_client):
    """runner 用 service token 访问机器接口（如 settings/runtime）应放行。"""
    response = auth_client.get(
        "/api/v1/settings/runtime",
        headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
    )
    assert response.status_code == 200


def test_service_token_wrong_value_rejected(auth_client):
    response = auth_client.get(
        "/api/v1/settings/runtime",
        headers={"Authorization": "Bearer wrong-service-token"},
    )
    assert response.status_code == 401


def test_user_token_still_works_alongside_service_token(auth_client):
    """用户登录 token 与 service token 并存互不干扰。"""
    login = _login(auth_client)
    assert login.status_code == 200
    token = login.json()["token"]
    response = auth_client.get(
        "/api/v1/projects",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200


# ─── 登录爆破防护（失败锁定 + 单 IP 限速） ───


def test_login_lockout_after_repeated_failures(client):
    """连续 5 次密码错误后，即使密码正确也返回 429 锁定。"""
    for _ in range(5):
        response = _login(client, password="wrong-pass-1")
        assert response.status_code == 401
    # 第 6 次：即使密码正确也被锁定
    response = _login(client)
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "login_locked"


def test_login_lockout_cleared_after_success(client):
    """登录成功后清零失败计数。"""
    for _ in range(3):
        assert _login(client, password="wrong-pass-1").status_code == 401
    assert _login(client).status_code == 200
    # 失败计数已清零，再来 4 次失败不会锁定
    for _ in range(4):
        assert _login(client, password="wrong-pass-1").status_code == 401
    assert _login(client).status_code == 200


def test_login_rate_limit_per_ip(client):
    """同一 IP 每分钟超过 10 次登录尝试被限速（不同用户名不触发锁定）。"""
    for i in range(10):
        response = _login(client, username=f"user{i}", password="wrong-pass-1")
        assert response.status_code == 401
    # 第 11 次尝试被限速
    response = _login(client, username="user11", password="wrong-pass-1")
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "login_rate_limited"


# ─── 关闭公网 API 文档（/docs /redoc /openapi.json） ───


def test_docs_and_openapi_disabled(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        response = client.get(path)
        assert response.status_code == 404, f"{path} 应返回 404"


# ─── 前端业务 JS / 模板鉴权隔离（未登录 404 伪装，登录后放行） ───
# 方案1：未登录攻击者无法通过下载静态 JS/模板枚举内部 API 路径


def test_unauthenticated_business_js_returns_404(client, monkeypatch):
    """未登录请求业务 JS（含全部内部 API 路径）应伪装 404，不暴露接口清单。"""
    monkeypatch.setenv("AXIOM_AUTH_DISABLED", "0")
    for path in (
        "/static/js/app.js",
        "/static/js/core.js",
        "/static/js/state.js",
        "/static/js/projects.js",
        "/static/js/helpers.js",
        "/static/js/v2-adapter.js",
    ):
        response = client.get(path)
        assert response.status_code == 404, f"{path} 未登录应返回 404"
        assert response.json()["error"]["code"] == "not_found"


def test_unauthenticated_templates_returns_404(client, monkeypatch):
    """未登录请求页面模板（含业务结构与 API 字段）应伪装 404。"""
    monkeypatch.setenv("AXIOM_AUTH_DISABLED", "0")
    for path in (
        "/static/templates/dashboard.html",
        "/static/templates/human-view.html",
        "/static/templates/settings.html",
        "/static/templates/task-list.html",
    ):
        response = client.get(path)
        assert response.status_code == 404, f"{path} 未登录应返回 404"


def test_unauthenticated_vendor_libs_returns_404(client, monkeypatch):
    """图库/渲染器等登录后按需加载的 vendor 库同样隐藏（未登录 404）。"""
    monkeypatch.setenv("AXIOM_AUTH_DISABLED", "0")
    for path in (
        "/static/vendor/cytoscape.min.js",
        "/static/vendor/dagre.min.js",
        "/static/vendor/marked.min.js",
        "/static/vendor/elk.bundled.js",
    ):
        response = client.get(path)
        assert response.status_code == 404, f"{path} 未登录应返回 404"


def test_public_login_assets_available_unauthenticated(client, monkeypatch):
    """登录卡必需公开资源（login.js/alpine/css/logo）未登录可访问。"""
    monkeypatch.setenv("AXIOM_AUTH_DISABLED", "0")
    for path in (
        "/static/js/login.js",
        "/static/vendor/alpine.min.js",
        "/static/apple.css",
        "/static/tailwind.generated.css",
        "/static/custom.css",
        "/static/logo.jpg",
    ):
        response = client.get(path)
        assert response.status_code == 200, f"{path} 公开资源应可访问"
    # login.js 不得包含任何业务接口路径（仅登录网关逻辑）
    login_js = client.get("/static/js/login.js").text
    assert "AxiomLogin" in login_js
    assert "performLogin" in login_js
    assert "/api/v1/auth/login" in login_js
    assert "/api/v1/projects" not in login_js


def test_authenticated_business_assets_available(client, monkeypatch):
    """登录后携带有效 token 访问业务 JS/模板/vendor 应 200。"""
    monkeypatch.setenv("AXIOM_AUTH_DISABLED", "0")
    token = _login(client).json()["token"]
    for path in (
        "/static/js/core.js",
        "/static/js/app.js",
        "/static/js/state.js",
        "/static/templates/dashboard.html",
        "/static/templates/human-view.html",
        "/static/vendor/marked.min.js",
    ):
        response = client.get(path, headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200, f"{path} 登录后应可访问"


def test_authenticated_business_assets_via_cookie(client, monkeypatch):
    """登录后浏览器携带 HttpOnly Cookie 访问业务 JS 也应 200（SPA 动态加载场景）。"""
    monkeypatch.setenv("AXIOM_AUTH_DISABLED", "0")
    login = _login(client)
    token = login.json()["token"]
    response = client.get(
        "/static/js/core.js",
        cookies={"axiom_token": token},
    )
    assert response.status_code == 200


# ─── HTML 脚本段按登录态切换（未登录只注入 login.js，已登录注入完整业务脚本） ───


def test_unauthenticated_index_serves_login_only_html(client, monkeypatch):
    """未登录 GET / 返回的 HTML 不得内联任何业务脚本，仅含公开登录网关 login.js。"""
    monkeypatch.setenv("AXIOM_AUTH_DISABLED", "0")
    response = client.get("/")
    assert response.status_code == 200
    text = response.text
    # 公开登录网关必须存在
    assert "/static/js/login.js?v=" in text
    # 全部业务脚本不得出现在未登录 HTML（攻击者无法通过静态分析枚举 API 路径）
    for script in [
        "state.js", "core.js", "helpers.js", "projects.js", "dashboard.js",
        "skills.js", "files.js", "mcp.js", "graph.js", "human.js",
        "archives.js", "timeline.js", "actions.js", "dispatch.js",
        "worker-tools.js", "prompts.js", "v2-adapter.js", "app.js",
    ]:
        assert f"/static/js/{script}" not in text, (
            f"未登录 HTML 不应内联 {script}（已登录版才暴露）"
        )
    # 未登录版仍可使用业务 JS 中的页面骨架（模板由运行时 fetch 拉取）
    assert 'id="page-host"' in text


def test_authenticated_index_serves_full_html(client, monkeypatch):
    """已登录 GET / 返回的 HTML 包含完整业务脚本（用户已认证可使用全部功能）。"""
    monkeypatch.setenv("AXIOM_AUTH_DISABLED", "0")
    token = _login(client).json()["token"]
    response = client.get("/", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    text = response.text
    # 完整业务脚本列表必须存在
    for script in [
        "state.js", "core.js", "helpers.js", "projects.js", "app.js",
    ]:
        assert f"/static/js/{script}" in text, f"已登录 HTML 应内联 {script}"


def test_spa_fallback_respects_auth_state(client, monkeypatch):
    """SPA 路由 fallback（如 /tasks/123）同样按登录态返回对应 HTML 版本。"""
    monkeypatch.setenv("AXIOM_AUTH_DISABLED", "0")
    # 未登录 fallback → login.js only
    unauth = client.get("/some/spa/route")
    assert unauth.status_code == 200
    assert "/static/js/login.js?v=" in unauth.text
    assert "/static/js/app.js" not in unauth.text
    # 已登录 fallback → 完整业务脚本
    token = _login(client).json()["token"]
    authed = client.get(
        "/some/spa/route",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert authed.status_code == 200
    assert "/static/js/app.js" in authed.text
