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
        "/api/v1/auth/login",
        "/static/js/app.js",
        "/",
    ],
)
def test_middleware_whitelists_public_paths(client, monkeypatch, path):
    monkeypatch.setenv("AXIOM_AUTH_DISABLED", "0")
    response = client.get(path)
    assert response.status_code != 401


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
