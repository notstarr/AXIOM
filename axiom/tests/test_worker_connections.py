from __future__ import annotations

import json
import re

import httpx

from axiom.modules.workers.tables import workers


def test_pi_worker_config_is_stored_in_one_canonical_shape(client) -> None:
    response = client.put(
        "/api/v1/workers/pi-main",
        json={
            "config": {
                "env": {
                    "PI_MODEL": "model-a",
                    "PI_BASE_URL": "https://models.example/v1",
                    "PI_API_KEY": "secret",
                    "PI_THINKING": "high",
                    "PI_MODEL_CONTEXT_WINDOW": "128000",
                    "HTTP_PROXY": "http://127.0.0.1:7890",
                }
            }
        },
    )

    assert response.status_code == 200, response.text
    config = response.json()["config"]
    assert "env" not in config
    assert config["model"] == "model-a"
    assert config["base_url"] == "https://models.example"
    assert config["api_key"] == "secret"
    assert config["thinking_enabled"] is True
    assert config["thinking_level"] == "high"
    assert "thinking" not in config
    assert config["context_window"] == 128000
    assert config["environment"] == {"HTTP_PROXY": "http://127.0.0.1:7890"}


def test_legacy_pi_worker_reads_with_split_thinking_controls(client, engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            workers.insert().values(
                name="legacy-pi",
                worker_type="pi",
                capabilities_json=json.dumps(["explore"]),
                max_running=1,
                priority=0,
                config_json=json.dumps(
                    {"model": "legacy-model", "thinking": "xhigh"}
                ),
                disabled=False,
                created_at="2026-08-03T00:00:00Z",
                updated_at="2026-08-03T00:00:00Z",
            )
        )

    response = client.get("/api/v1/workers/legacy-pi")

    assert response.status_code == 200, response.text
    config = response.json()["config"]
    assert config["thinking_enabled"] is True
    assert config["thinking_level"] == "xhigh"
    assert "thinking" not in config


def test_worker_models_uses_saved_connection_and_normalizes_response(client, monkeypatch) -> None:
    client.put(
        "/api/v1/workers/pi-main",
        json={
            "config": {
                "model": "model-a",
                "base_url": "https://models.example/v1",
                "api_key": "secret",
            }
        },
    )
    captured = {}

    def fake_get(url, *, headers, timeout, proxy=None):
        captured.update(url=url, headers=headers, timeout=timeout, proxy=proxy)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "model-a", "context_length": 131072},
                    {"name": "model-b"},
                    {"id": "model-a"},
                ]
            },
        )

    monkeypatch.setattr("axiom.modules.workers.service.httpx.get", fake_get)
    response = client.get("/api/v1/workers/pi-main/models")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "models": [
            {"id": "model-a", "name": "model-a", "context_window": 131072},
            {"id": "model-b", "name": "model-b", "context_window": 200000},
        ],
        "error": "",
    }
    assert {k: captured[k] for k in ('url', 'headers', 'timeout')} == {
        "url": "https://models.example/v1/models",
        "headers": {"Authorization": "Bearer secret"},
        "timeout": 10,
    }


def test_worker_models_uses_custom_headers(client, monkeypatch) -> None:
    client.put(
        "/api/v1/workers/pi-main",
        json={
            "config": {
                "model": "model-a",
                "base_url": "https://models.example",
                "api_key": "secret",
                "headers": {"X-Tenant": "acme"},
            }
        },
    )
    captured = {}

    def fake_get(url, *, headers, timeout, proxy=None):
        captured.update(headers=headers)
        return httpx.Response(200, json={"data": [{"id": "model-a"}]})

    monkeypatch.setattr("axiom.modules.workers.service.httpx.get", fake_get)
    response = client.get("/api/v1/workers/pi-main/models")

    assert response.status_code == 200, response.text
    assert captured["headers"]["X-Tenant"] == "acme"
    assert captured["headers"]["Authorization"] == "Bearer secret"


def test_opencode_go_session_is_persisted_and_user_value_wins(client) -> None:
    config = {
        "model": "kimi-k2.6",
        "base_url": "https://opencode.ai/inference/go",
        "api_key": "sk-test",
    }
    response = client.put("/api/v1/workers/opencode-go", json={"config": config})
    assert response.status_code == 200, response.text
    generated = response.json()["config"]["headers"]["x-opencode-session"]
    assert re.fullmatch(r"Rat-[0-9a-z]{9}", generated)

    response = client.put(
        "/api/v1/workers/opencode-go",
        json={
            "config": {
                **config,
                "headers": {"x-opencode-session": "Rat-user12345"},
            }
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["config"]["headers"]["x-opencode-session"] == "Rat-user12345"

    response = client.put(
        "/api/v1/workers/opencode-go",
        json={"config": {**config, "headers": {"x-opencode-session": ""}}},
    )
    assert response.status_code == 200, response.text
    assert re.fullmatch(
        r"Rat-[0-9a-z]{9}",
        response.json()["config"]["headers"]["x-opencode-session"],
    )


def test_draft_models_request_fills_opencode_session(client, monkeypatch) -> None:
    captured = {}

    def fake_get(url, *, headers, timeout, proxy=None):
        captured.update(url=url, headers=headers)
        return httpx.Response(200, json={"data": [{"id": "kimi-k2.6"}]})

    monkeypatch.setattr("axiom.modules.workers.service.httpx.get", fake_get)
    response = client.get(
        "/api/v1/workers/__draft__/models",
        params={
            "base_url": "https://opencode.ai/inference/go",
            "headers": json.dumps({"X-Test": "1"}),
        },
    )

    assert response.status_code == 200, response.text
    assert captured["url"] == "https://opencode.ai/inference/go/v1/models"
    assert captured["headers"]["X-Test"] == "1"
    assert re.fullmatch(r"Rat-[0-9a-z]{9}", captured["headers"]["x-opencode-session"])


def test_worker_connection_sends_custom_headers(client, monkeypatch) -> None:
    captured = {}

    def fake_post(url, *, headers, json, timeout, proxy=None):
        captured.update(headers=headers)
        return httpx.Response(200, json={"id": "response"})

    monkeypatch.setattr("axiom.modules.workers.service.httpx.post", fake_post)
    response = client.post(
        "/api/v1/workers/__guard__/test",
        json={
            "base_url": "https://models.example",
            "api_key": "guard-secret",
            "model": "guard-model",
            "headers": {"X-Tenant": "acme"},
        },
    )

    assert response.status_code == 200, response.text
    assert captured["headers"]["X-Tenant"] == "acme"
    assert captured["headers"]["Authorization"] == "Bearer guard-secret"


def test_worker_connection_supports_responses_api_and_unsaved_guard(client, monkeypatch) -> None:
    captured = {}

    def fake_post(url, *, headers, json, timeout, proxy=None):
        captured.update(url=url, headers=headers, json=json, timeout=timeout)
        return httpx.Response(200, json={"id": "response"})

    monkeypatch.setattr("axiom.modules.workers.service.httpx.post", fake_post)
    response = client.post(
        "/api/v1/workers/__guard__/test",
        json={
            "base_url": "https://models.example/v1",
            "api_key": "guard-secret",
            "model": "guard-model",
            "provider_api": "openai-responses",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "message": "连接成功，模型可用"}
    assert captured["url"] == "https://models.example/v1/responses"
    assert captured["json"] == {
        "model": "guard-model",
        "input": "ping",
        "max_output_tokens": 8,
    }


def test_worker_connection_supports_anthropic_messages(client, monkeypatch) -> None:
    captured = {}

    def fake_post(url, *, headers, json, timeout, proxy=None):
        captured.update(url=url, headers=headers, json=json, timeout=timeout)
        return httpx.Response(200, json={"id": "message"})

    monkeypatch.setattr("axiom.modules.workers.service.httpx.post", fake_post)
    response = client.post(
        "/api/v1/workers/__guard__/test",
        json={
            "base_url": "https://models.example",
            "api_key": "anthropic-secret",
            "model": "claude-test",
            "provider_api": "anthropic-messages",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "message": "连接成功，模型可用"}
    assert captured["url"] == "https://models.example/v1/messages"
    assert captured["headers"]["x-api-key"] == "anthropic-secret"
    assert captured["headers"]["anthropic-version"] == "2023-06-01"
    assert "authorization" not in {key.lower() for key in captured["headers"]}
    assert captured["json"] == {
        "model": "claude-test",
        "max_tokens": 8,
        "messages": [{"role": "user", "content": "ping"}],
    }


def test_anthropic_base_url_with_v1_uses_relative_messages_path(client, monkeypatch) -> None:
    captured = {}

    def fake_post(url, *, headers, json, timeout, proxy=None):
        captured.update(url=url, headers=headers, json=json)
        return httpx.Response(200, json={"id": "message"})

    monkeypatch.setattr("axiom.modules.workers.service.httpx.post", fake_post)
    response = client.post(
        "/api/v1/workers/__guard__/test",
        json={
            "base_url": "https://models.example/v1",
            "api_key": "anthropic-secret",
            "model": "claude-test",
            "provider_api": "anthropic-messages",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "message": "连接成功，模型可用"}
    assert captured["url"] == "https://models.example/v1/messages"


def test_anthropic_worker_models_use_v1_models_and_api_key(client, monkeypatch) -> None:
    client.put(
        "/api/v1/workers/anthropic-worker",
        json={
            "config": {
                "model": "claude-test",
                "base_url": "https://models.example",
                "provider_api": "anthropic-messages",
                "api_key": "anthropic-secret",
            }
        },
    )
    captured = {}

    def fake_get(url, *, headers, timeout, proxy=None):
        captured.update(url=url, headers=headers, timeout=timeout)
        return httpx.Response(200, json={"data": [{"id": "claude-test"}]})

    monkeypatch.setattr("axiom.modules.workers.service.httpx.get", fake_get)
    response = client.get("/api/v1/workers/anthropic-worker/models")

    assert response.status_code == 200, response.text
    assert response.json()["models"][0]["id"] == "claude-test"
    assert captured["url"] == "https://models.example/v1/models"
    assert captured["headers"] == {
        "x-api-key": "anthropic-secret",
        "anthropic-version": "2023-06-01",
    }
