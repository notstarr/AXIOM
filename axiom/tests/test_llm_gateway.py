"""LLM 网关（/api/v1/llm-gateway）与 Pi models.json 网关改写测试。"""

from __future__ import annotations

import httpx

from axiom.dispatcher.pi import PiBootstrapExecutor
from axiom.modules.workers.schemas import Worker


def _worker(
    name: str = "gw-test",
    base_url: str = "",
    api_key: str = "sk-real",
    provider_api: str = "openai-completions",
) -> Worker:
    return Worker(
        name=name,
        worker_type="pi",
        capabilities=["bootstrap"],
        max_running=1,
        priority=1,
        config={
            "model": "test-model",
            "base_url": base_url,
            "provider_api": provider_api,
            "api_key": api_key,
            "context_window": 128000,
        },
        disabled=False,
        cooldown_until=None,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )


def _upsert_body(
    base_url: str = "",
    api_key: str = "sk-real",
    provider_api: str = "openai-completions",
    llm_proxy: bool = False,
) -> dict:
    return {
        "worker_type": "pi",
        "capabilities": ["bootstrap"],
        "config": {
            "model": "test-model",
            "base_url": base_url,
            "provider_api": provider_api,
            "api_key": api_key,
            "context_window": 128000,
            "llm_proxy": llm_proxy,
        },
    }


# ── 网关端点 ──


def test_gateway_requires_configured_base_url(client):
    client.put("/api/v1/workers/gw-empty", json=_upsert_body())
    response = client.post("/api/v1/llm-gateway/gw-empty/chat/completions", json={})
    assert response.status_code == 400


def test_gateway_unknown_worker_returns_404(client):
    response = client.post("/api/v1/llm-gateway/no-such-worker/chat/completions", json={})
    assert response.status_code == 404


def test_gateway_forwards_with_injected_key(client, monkeypatch):
    """直连模式（proxy off）：请求转发到上游，且注入 worker 的真实 key。"""
    seen: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = (await request.aread()).decode()
        seen["path"] = request.url.path

        async def stream():
            yield b'{"ok": true, "model": "test-model"}'

        return httpx.Response(200, content=stream())

    real_client = httpx.AsyncClient

    def fake_async_client(*args, **kwargs):
        assert kwargs.get("proxy") is None, "proxy off 模式不应配置代理"
        return real_client(transport=httpx.MockTransport(handler), **{k: v for k, v in kwargs.items() if k != "proxy"})

    monkeypatch.setattr("axiom.modules.llm_gateway.api.httpx.AsyncClient", fake_async_client)

    client.put(
        "/api/v1/workers/gw-test",
        json=_upsert_body(base_url="http://upstream.test/v1"),
    )
    response = client.post(
        "/api/v1/llm-gateway/gw-test/chat/completions",
        json={"model": "test-model", "messages": []},
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert seen["auth"] == "Bearer sk-real"
    assert seen["path"] == "/v1/chat/completions"


def test_gateway_forwards_anthropic_with_x_api_key(client, monkeypatch):
    """Anthropic requests must not receive the Pi service token as auth."""
    seen: dict = {}
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("Authorization")
        seen["api_key"] = request.headers.get("x-api-key")
        seen["version"] = request.headers.get("anthropic-version")
        seen["path"] = request.url.path

        async def stream():
            yield b'{"id":"message"}'

        return httpx.Response(200, content=stream())

    real_client = httpx.AsyncClient

    def fake_async_client(*args, **kwargs):
        captured["proxy"] = kwargs.get("proxy")
        return real_client(
            transport=httpx.MockTransport(handler),
            **{k: v for k, v in kwargs.items() if k != "proxy"},
        )

    monkeypatch.setattr("axiom.modules.llm_gateway.api.httpx.AsyncClient", fake_async_client)

    client.put(
        "/api/v1/workers/gw-anthropic",
        json=_upsert_body(
            base_url="http://upstream.test",
            provider_api="anthropic-messages",
            llm_proxy=True,
        ),
    )
    settings = client.get("/api/v1/settings/runtime").json()
    config = settings["config"] if "config" in settings else settings
    config["network_proxy_mode"] = "http"
    config["network_proxy_url"] = "http://127.0.0.1:7890"
    updated = client.put(
        "/api/v1/settings/runtime",
        json={"config": config, "expected_version": settings.get("version")},
    ).json()
    response = client.post(
        "/api/v1/llm-gateway/gw-anthropic/v1/messages",
        headers={"Authorization": "Bearer svc-token", "x-api-key": "svc-token"},
        json={"model": "test-model", "messages": []},
    )

    assert response.status_code == 200
    assert seen == {
        "authorization": None,
        "api_key": "sk-real",
        "version": "2023-06-01",
        "path": "/v1/messages",
    }
    assert captured["proxy"] == "http://127.0.0.1:7890"

    config["network_proxy_mode"] = "off"
    client.put(
        "/api/v1/settings/runtime",
        json={"config": config, "expected_version": updated.get("version")},
    )


def test_gateway_applies_proxy_when_enabled(client, monkeypatch):
    """proxy http 模式：AsyncClient 收到代理地址；不可达代理 → 502。"""
    captured: dict = {}

    real_client = httpx.AsyncClient

    def fake_async_client(*args, **kwargs):
        captured["proxy"] = kwargs.get("proxy")
        async def passthrough(request: httpx.Request) -> httpx.Response:
            async def empty():
                yield b"{}"

            return httpx.Response(200, content=empty())

        return real_client(transport=httpx.MockTransport(passthrough), **{k: v for k, v in kwargs.items() if k != "proxy"})

    monkeypatch.setattr("axiom.modules.llm_gateway.api.httpx.AsyncClient", fake_async_client)

    client.put(
        "/api/v1/workers/gw-test",
        json=_upsert_body(base_url="http://upstream.test/v1", llm_proxy=True),
    )
    settings_response = client.get("/api/v1/settings/runtime")
    settings = settings_response.json()
    config = settings["config"] if "config" in settings else settings
    config["network_proxy_mode"] = "http"
    config["network_proxy_url"] = "http://127.0.0.1:7890"
    payload = {"config": config, "expected_version": settings.get("version")}
    update = client.put("/api/v1/settings/runtime", json=payload)
    assert update.status_code == 200, update.text

    client.post("/api/v1/llm-gateway/gw-test/chat/completions", json={})
    assert captured["proxy"] == "http://127.0.0.1:7890"

    # 还原为 off，避免影响其他用例
    config["network_proxy_mode"] = "off"
    client.put("/api/v1/settings/runtime", json={"config": config, "expected_version": payload["expected_version"]})


# ── Pi models.json 网关改写 ──


def _executor(tmp_path, *, gateway_enabled: bool):
    return PiBootstrapExecutor(
        _worker(base_url="https://api.real-provider.com/v1"),
        api_url="http://127.0.0.1:8888",
        service_token="svc-token-123",
        llm_gateway_enabled=gateway_enabled,
    )


def test_models_file_points_to_gateway_when_enabled(tmp_path):
    executor = _executor(tmp_path, gateway_enabled=True)
    agent_dir = tmp_path / "agent"
    executor._write_models_file(agent_dir)
    import json

    payload = json.loads((agent_dir / "models.json").read_text())
    provider = payload["providers"]["axiom"]
    assert provider["baseUrl"] == "http://127.0.0.1:8888/api/v1/llm-gateway/gw-test"
    assert provider["apiKey"] == "svc-token-123"


def test_models_file_keeps_real_key_when_gateway_disabled(tmp_path):
    executor = _executor(tmp_path, gateway_enabled=False)
    agent_dir = tmp_path / "agent"
    executor._write_models_file(agent_dir)
    import json

    payload = json.loads((agent_dir / "models.json").read_text())
    provider = payload["providers"]["axiom"]
    assert provider["baseUrl"] == "https://api.real-provider.com/v1"
    assert provider["apiKey"] == "sk-real"


def test_models_file_keeps_anthropic_api_and_base_url(tmp_path):
    executor = PiBootstrapExecutor(
        _worker(
            base_url="https://api.real-provider.com",
            provider_api="anthropic-messages",
        ),
        api_url="http://127.0.0.1:8888",
        service_token="svc-token-123",
        llm_gateway_enabled=False,
    )
    agent_dir = tmp_path / "agent"
    executor._write_models_file(agent_dir)
    import json

    provider = json.loads((agent_dir / "models.json").read_text())["providers"]["axiom"]
    assert provider["api"] == "anthropic-messages"
    assert provider["baseUrl"] == "https://api.real-provider.com"


def test_models_file_normalizes_legacy_responses_api(tmp_path):
    executor = PiBootstrapExecutor(
        _worker(
            base_url="https://api.real-provider.com/v1",
            provider_api="responses",
        ),
        api_url="http://127.0.0.1:8888",
        service_token="svc-token-123",
        llm_gateway_enabled=False,
    )
    agent_dir = tmp_path / "agent"
    executor._write_models_file(agent_dir)
    import json

    provider = json.loads((agent_dir / "models.json").read_text())["providers"]["axiom"]
    assert provider["api"] == "openai-responses"


# ── WorkerService 模型发现走代理 ──


def test_list_models_uses_proxy_when_enabled(client, monkeypatch):
    captured: dict = {}

    def fake_get(url, **kwargs):
        captured["proxy"] = kwargs.get("proxy")
        request = httpx.Request("GET", url)
        return httpx.Response(200, request=request, json={"data": []})

    monkeypatch.setattr(httpx, "get", fake_get)

    client.put(
        "/api/v1/workers/gw-proxy",
        json=_upsert_body(base_url="http://upstream.test/v1", llm_proxy=True),
    )

    settings_response = client.get("/api/v1/settings/runtime")
    settings = settings_response.json()
    config = settings["config"] if "config" in settings else settings
    config["network_proxy_mode"] = "http"
    config["network_proxy_url"] = "http://127.0.0.1:7890"
    updated = client.put(
        "/api/v1/settings/runtime",
        json={"config": config, "expected_version": settings.get("version")},
    ).json()
    current_version = updated.get("version")

    client.get("/api/v1/workers/gw-proxy/models")
    assert captured["proxy"] == "http://127.0.0.1:7890"

    config["network_proxy_mode"] = "off"
    client.put("/api/v1/settings/runtime", json={"config": config, "expected_version": current_version})
    client.get("/api/v1/workers/gw-proxy/models")
    assert captured["proxy"] is None


def test_test_connection_uses_proxy_when_enabled(client, monkeypatch):
    """Worker 测试连接（chat/completions ping）同样走代理。"""
    captured: dict = {}

    def fake_post(url, *, headers, json, timeout, proxy=None):
        captured["proxy"] = proxy
        request = httpx.Request("POST", url)
        return httpx.Response(200, request=request, json={"choices": [{"message": {"content": "pong"}}]})

    monkeypatch.setattr(httpx, "post", fake_post)

    client.put(
        "/api/v1/workers/gw-proxy",
        json=_upsert_body(base_url="http://upstream.test/v1", llm_proxy=True),
    )

    settings_response = client.get("/api/v1/settings/runtime")
    settings = settings_response.json()
    config = settings["config"] if "config" in settings else settings
    config["network_proxy_mode"] = "http"
    config["network_proxy_url"] = "http://127.0.0.1:7890"
    updated = client.put(
        "/api/v1/settings/runtime",
        json={"config": config, "expected_version": settings.get("version")},
    ).json()
    config["network_proxy_mode"] = "off"

    client.post("/api/v1/workers/gw-proxy/test", json={"base_url": "http://upstream.test/v1", "model": "test-model"})
    assert captured["proxy"] == "http://127.0.0.1:7890"

    client.put("/api/v1/settings/runtime", json={"config": config, "expected_version": updated.get("version")})
    client.post("/api/v1/workers/gw-proxy/test", json={"base_url": "http://upstream.test/v1", "model": "test-model"})
    assert captured["proxy"] is None
