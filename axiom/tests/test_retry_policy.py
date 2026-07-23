from __future__ import annotations

from sqlalchemy import update

from axiom.modules.graph.tables import intents


def _configure(client, **changes) -> None:
    current = client.get("/api/v1/settings/runtime").json()
    config = current["config"]
    config.update(changes)
    response = client.put(
        "/api/v1/settings/runtime",
        json={"actor": "test", "config": config},
    )
    assert response.status_code == 200, response.text


def _claimed(client) -> tuple[dict, dict, str]:
    project = client.post("/api/v1/projects", json={"title": "重试策略"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={"project_id": project["id"], "title": "重试", "origin": "a.example.com"},
    ).json()
    client.put(
        "/api/v1/workers/worker-a",
        json={"capabilities": ["bootstrap"], "max_running": 1},
    )
    base = f"/api/v1/dispatch/tasks/{task['id']}/intents/i001"
    claim = client.post(f"{base}/claim", json={"worker_name": "worker-a"}).json()
    client.post(f"{base}/start", json={"lease_id": claim["lease_id"]})
    return task, claim, base


def test_execution_failures_use_server_owned_backoff_and_stop_at_limit(client, engine) -> None:
    _configure(
        client,
        execution_retry_limit=2,
        retry_base_seconds=7,
        retry_max_seconds=30,
    )
    task, claim, base = _claimed(client)
    first = client.post(
        f"{base}/release",
        json={
            "lease_id": claim["lease_id"],
            "failure_kind": "invalid_output",
            "failure_class": "execution",
            "next_retry_at": "2020-01-01T00:00:00Z",
        },
    )
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "pending"
    assert first.json()["retry_count"] == 1
    assert first.json()["next_retry_at"] != "2020-01-01T00:00:00Z"
    assert client.post(f"{base}/claim", json={"worker_name": "worker-a"}).status_code == 409

    with engine.begin() as conn:
        conn.execute(
            update(intents)
            .where(intents.c.task_id == task["id"], intents.c.id == "i001")
            .values(next_retry_at="2020-01-01T00:00:00Z")
        )
    claim2 = client.post(f"{base}/claim", json={"worker_name": "worker-a"}).json()
    client.post(f"{base}/start", json={"lease_id": claim2["lease_id"]})
    second = client.post(
        f"{base}/release",
        json={
            "lease_id": claim2["lease_id"],
            "failure_kind": "invalid_output",
            "failure_class": "execution",
        },
    )
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "failed"
    assert second.json()["retry_count"] == 2
    assert second.json()["next_retry_at"] is None
    intent = client.get(f"/api/v1/tasks/{task['id']}/graph").json()["intents"][0]
    assert intent["status"] == "failed"


def test_transient_failure_consumes_only_infrastructure_budget_and_cools_worker(client) -> None:
    _configure(client, worker_failure_cooldown_seconds=45)
    _task, claim, base = _claimed(client)
    response = client.post(
        f"{base}/release",
        json={
            "lease_id": claim["lease_id"],
            "failure_kind": "provider_unavailable",
            "failure_class": "transient",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["retry_count"] == 0
    assert response.json()["infrastructure_retry_count"] == 1
    worker = client.get("/api/v1/workers/worker-a").json()
    assert worker["cooldown_until"] is not None
