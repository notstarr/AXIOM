from __future__ import annotations

from sqlalchemy import update

from axiom.modules.graph.tables import intents


def test_stale_lease_is_recovered_and_pending_intent_can_be_claimed_again(client, engine) -> None:
    project = client.post("/api/v1/projects", json={"title": "系统"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "Lease recovery",
            "origin": "a.example.com",
            "goal": "验证过期租约恢复",
            "worker_pool": ["worker-a"],
        },
    ).json()
    client.put(
        "/api/v1/workers/worker-a",
        json={"capabilities": ["bootstrap"], "max_running": 1},
    )
    first = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/intents/i001/claim",
        json={"worker_name": "worker-a"},
    ).json()
    base = f"/api/v1/dispatch/tasks/{task['id']}/intents/i001"
    client.post(f"{base}/start", json={"lease_id": first["lease_id"]})
    with engine.begin() as conn:
        conn.execute(
            update(intents)
            .where(intents.c.task_id == task["id"], intents.c.id == "i001")
            .values(last_heartbeat_at="2026-07-23T00:00:00Z")
        )

    recovered = client.post(
        "/api/v1/dispatch/recover-stale",
        json={"stale_after_seconds": 60},
    )
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["recovered"] == [
        {
            "task_id": task["id"],
            "intent_id": "i001",
            "worker_name": "worker-a",
            "expired_lease_id": first["lease_id"],
        }
    ]

    stale_heartbeat = client.post(f"{base}/heartbeat", json={"lease_id": first["lease_id"]})
    assert stale_heartbeat.status_code == 409
    intent = client.get(f"/api/v1/tasks/{task['id']}/graph").json()["intents"][0]
    assert intent["status"] == "pending"
    assert intent["infrastructure_retry_count"] == 1
    assert intent["last_failure_kind"] == "lease_expired"

    # Recovery deliberately applies the same server-owned backoff as a worker
    # failure; make the retry eligible to verify the next claim separately.
    with engine.begin() as conn:
        conn.execute(
            update(intents)
            .where(intents.c.task_id == task["id"], intents.c.id == "i001")
            .values(next_retry_at="2026-01-01T00:00:00Z")
        )

    second = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/intents/i001/claim",
        json={"worker_name": "worker-a"},
    )
    assert second.status_code == 200, second.text
    assert second.json()["lease_id"] != first["lease_id"]


def test_fresh_lease_is_not_recovered(client) -> None:
    project = client.post("/api/v1/projects", json={"title": "系统"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "Fresh lease",
            "origin": "a.example.com",
            "goal": "验证活动租约保留",
        },
    ).json()
    client.put(
        "/api/v1/workers/worker-a",
        json={"capabilities": ["bootstrap"], "max_running": 1},
    )
    client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/intents/i001/claim",
        json={"worker_name": "worker-a"},
    )

    recovered = client.post(
        "/api/v1/dispatch/recover-stale",
        json={"stale_after_seconds": 60},
    )

    assert recovered.status_code == 200
    assert recovered.json() == {"recovered": [], "recovered_reason_runs": []}
