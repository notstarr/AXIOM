from __future__ import annotations

from axiom.modules.graph.tables import intents


def _project_and_task(client) -> tuple[dict, dict]:
    project = client.post("/api/v1/projects", json={"title": "重试恢复测试"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "重试预算恢复",
            "origin": "retry.example.com",
        },
    ).json()
    return project, task


def _stuck_values(**overrides):
    values = {
        "kind": "explore",
        "status": "pending",
        "retry_count": 4,
        "infrastructure_retry_count": 5,
        "last_failure_kind": "worker_infrastructure",
        "failure_class": "transient",
        "activity_kind": "retry_blocked",
        "activity_summary": "基础设施重试已达上限，等待人工或系统恢复：worker_infrastructure",
        "activity_tool": "",
        "next_retry_at": None,
    }
    values.update(overrides)
    return values


def _set_intent(engine, task_id: str, intent_id: str, values: dict) -> None:
    with engine.begin() as conn:
        conn.execute(
            intents.update()
            .where(intents.c.task_id == task_id, intents.c.id == intent_id)
            .values(**values)
        )


def test_reset_retries_clears_budget_and_unblocks_claim(client, engine) -> None:
    _project, task = _project_and_task(client)
    client.put(
        "/api/v1/workers/worker-a",
        json={"capabilities": ["explore"], "max_running": 1},
    )
    _set_intent(engine, task["id"], "i001", _stuck_values())

    claim_endpoint = f"/api/v1/dispatch/tasks/{task['id']}/intents/i001/claim"
    blocked = client.post(claim_endpoint, json={"worker_name": "worker-a"})
    assert blocked.status_code == 409

    reset = client.post(f"/api/v1/tasks/{task['id']}/intents/i001/reset-retries")
    assert reset.status_code == 200, reset.text
    body = reset.json()
    assert body["retry_count"] == 0
    assert body["infrastructure_retry_count"] == 0
    assert body["failure_class"] == ""
    assert body["last_failure_kind"] == ""
    assert body["activity_kind"] == ""
    assert body["activity_summary"] == ""
    assert body["next_retry_at"] is None
    assert body["status"] == "pending"

    claimed = client.post(claim_endpoint, json={"worker_name": "worker-a"})
    assert claimed.status_code == 200, claimed.text


def test_reset_retries_is_idempotent_when_intent_has_no_retry_debris(
    client, engine
) -> None:
    _project, task = _project_and_task(client)
    reset = client.post(f"/api/v1/tasks/{task['id']}/intents/i001/reset-retries")
    assert reset.status_code == 200, reset.text
    assert reset.json()["status"] == "pending"
    assert reset.json()["retry_count"] == 0


def test_reset_retries_rejects_non_pending_intent(client, engine) -> None:
    _project, task = _project_and_task(client)
    _set_intent(
        engine,
        task["id"],
        "i001",
        _stuck_values(status="running"),
    )
    reset = client.post(f"/api/v1/tasks/{task['id']}/intents/i001/reset-retries")
    assert reset.status_code == 409
