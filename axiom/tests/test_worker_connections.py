from __future__ import annotations

import httpx


def test_pi_worker_config_is_stored_in_one_canonical_shape(client) -> None:
    response = client.put(
        "/api/v1/workers/pi-main",
        json={
            "config": {
                "env": {
                    "PI_MODEL": "model-a",
                    "PI_BASE_URL": "https://models.example/v1",
                    "PI_API_KEY": "secret",
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
    assert config["base_url"] == "https://models.example/v1"
    assert config["api_key"] == "secret"
    assert config["context_window"] == 128000
    assert config["environment"] == {"HTTP_PROXY": "http://127.0.0.1:7890"}


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

    def fake_get(url, *, headers, timeout):
        captured.update(url=url, headers=headers, timeout=timeout)
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
    assert captured == {
        "url": "https://models.example/v1/models",
        "headers": {"Authorization": "Bearer secret"},
        "timeout": 10,
    }


def test_worker_connection_supports_responses_api_and_unsaved_guard(client, monkeypatch) -> None:
    captured = {}

    def fake_post(url, *, headers, json, timeout):
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
