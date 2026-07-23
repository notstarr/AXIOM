from __future__ import annotations

from axiom.dispatcher.loop import Dispatcher
from axiom.dispatcher.schemas import ClaimedIntent, DispatcherSnapshot


class ClientControlPlane:
    def __init__(self, client):
        self.client = client

    def snapshot(self) -> DispatcherSnapshot:
        response = self.client.get("/api/v1/dispatch/snapshot")
        assert response.status_code == 200, response.text
        return DispatcherSnapshot.model_validate(response.json())

    def claim(self, *, task_id: str, intent_id: str, worker_name: str) -> ClaimedIntent | None:
        response = self.client.post(
            f"/api/v1/dispatch/tasks/{task_id}/intents/{intent_id}/claim",
            json={"worker_name": worker_name},
        )
        if response.status_code == 409:
            return None
        assert response.status_code == 200, response.text
        return ClaimedIntent.model_validate(response.json())


def test_dispatcher_claims_bootstrap_for_an_allowed_idle_worker(client) -> None:
    project = client.post("/api/v1/projects", json={"title": "系统"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "测试",
            "origin": "a.example.com",
            "goal": "完成渗透测试",
            "worker_pool": ["worker-b"],
        },
    ).json()
    for name, priority in (("worker-a", 0), ("worker-b", 10)):
        response = client.put(
            f"/api/v1/workers/{name}",
            json={
                "capabilities": ["bootstrap", "reason", "explore"],
                "max_running": 1,
                "priority": priority,
            },
        )
        assert response.status_code == 200, response.text

    report = Dispatcher(ClientControlPlane(client)).tick()

    assert report.claimed == 1
    graph = client.get(f"/api/v1/tasks/{task['id']}/graph").json()
    assert graph["intents"][0]["status"] == "claimed"
    assert graph["intents"][0]["worker"] == "worker-b"
    assert graph["intents"][0]["lease_id"].startswith("lease_")


def test_claim_rechecks_capacity_and_pending_state_inside_transaction(client) -> None:
    project = client.post("/api/v1/projects", json={"title": "系统"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "测试",
            "origin": "a.example.com",
            "goal": "完成渗透测试",
        },
    ).json()
    client.put(
        "/api/v1/workers/worker-a",
        json={"capabilities": ["bootstrap"], "max_running": 1},
    )
    endpoint = f"/api/v1/dispatch/tasks/{task['id']}/intents/i001/claim"

    assert client.post(endpoint, json={"worker_name": "worker-a"}).status_code == 200
    conflict = client.post(endpoint, json={"worker_name": "worker-a"})

    assert conflict.status_code == 409
    assert conflict.json()["error"]["message"] == "Intent is no longer pending"


def test_stop_invalidates_the_claim_and_resume_makes_intent_dispatchable_again(client) -> None:
    project = client.post("/api/v1/projects", json={"title": "系统"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "测试",
            "origin": "a.example.com",
            "goal": "完成渗透测试",
        },
    ).json()
    client.put(
        "/api/v1/workers/worker-a",
        json={"capabilities": ["bootstrap"], "max_running": 1},
    )
    endpoint = f"/api/v1/dispatch/tasks/{task['id']}/intents/i001/claim"
    first_claim = client.post(endpoint, json={"worker_name": "worker-a"}).json()

    assert client.post(f"/api/v1/tasks/{task['id']}/stop").status_code == 200
    stopped_intent = client.get(f"/api/v1/tasks/{task['id']}/graph").json()["intents"][0]
    assert stopped_intent["status"] == "pending"
    assert stopped_intent["worker"] is None
    assert stopped_intent["lease_id"] is None

    assert client.post(f"/api/v1/tasks/{task['id']}/resume").status_code == 200
    second_claim = client.post(endpoint, json={"worker_name": "worker-a"}).json()
    assert second_claim["lease_id"] != first_claim["lease_id"]
