from __future__ import annotations

import pytest

from axiom.dispatcher.loop import Dispatcher
from axiom.dispatcher.schemas import ClaimedIntent, DispatcherSnapshot
from protocol_helpers import (
    claim_reason_run,
    complete_bootstrap,
    open_reason_workspace,
    update_runtime_settings,
)


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


def _bootstrap_task(client, *, project_id: str, title: str, worker_name: str) -> dict:
    return client.post(
        "/api/v1/tasks",
        json={
            "project_id": project_id,
            "title": title,
            "origin": f"{title}.example.com",
            "goal": "完成渗透测试",
            "worker_pool": [worker_name],
        },
    ).json()


def _task_with_two_pending_explores(client, **runtime_changes) -> tuple[dict, list[str]]:
    update_runtime_settings(client, **runtime_changes)
    project = client.post("/api/v1/projects", json={"title": "并发事务"}).json()
    task = _bootstrap_task(
        client,
        project_id=project["id"],
        title="explore-limits",
        worker_name="worker-a",
    )
    client.put(
        "/api/v1/workers/worker-a",
        json={"capabilities": ["bootstrap", "reason", "explore"], "max_running": 4},
    )
    bootstrap_base = f"/api/v1/dispatch/tasks/{task['id']}/intents/i001"
    bootstrap_claim = client.post(
        f"{bootstrap_base}/claim",
        json={"worker_name": "worker-a"},
    ).json()
    assert client.post(
        f"{bootstrap_base}/start",
        json={"lease_id": bootstrap_claim["lease_id"]},
    ).status_code == 200
    completed = complete_bootstrap(
        client,
        task_id=task["id"],
        intent_id="i001",
        lease_id=bootstrap_claim["lease_id"],
    )
    reason_claim, _execution = claim_reason_run(
        client,
        task_id=task["id"],
        reason_run_id=completed["next_reason_run_id"],
    )
    workspace = open_reason_workspace(client, task_id=task["id"], claim=reason_claim)
    for number in (1, 2):
        todo_ref = f"todo-{number}"
        todo = client.post(
            f"/api/v1/reason-workspaces/{workspace['id']}/todos",
            json={
                "lease_id": reason_claim["lease_id"],
                "todo": {
                    "ref": todo_ref,
                    "title": f"验证方向 {number}",
                    "category": "authorization",
                    "sources": [{"type": "fact", "id": completed["fact_id"]}],
                },
            },
        )
        assert todo.status_code == 200, todo.text
        proposal = client.post(
            f"/api/v1/reason-workspaces/{workspace['id']}/intents",
            json={
                "lease_id": reason_claim["lease_id"],
                "intent": {
                    "todo_ref": todo_ref,
                    "from_facts": [completed["fact_id"]],
                    "description": f"验证方向 {number}",
                    "targets": ["explore-limits.example.com"],
                    "execute_seconds": 300,
                    "conclude_seconds": 180,
                },
            },
        )
        assert proposal.status_code == 200, proposal.text
    assessment = client.post(
        f"/api/v1/reason-workspaces/{workspace['id']}/goal-assessment",
        json={
            "lease_id": reason_claim["lease_id"],
            "assessment": {
                "achieved": False,
                "reason": "继续执行两个 Explore",
                "evidence_refs": [f"fact:{completed['fact_id']}"],
            },
        },
    )
    assert assessment.status_code == 200, assessment.text
    committed = client.post(
        f"/api/v1/reason-workspaces/{workspace['id']}/commit",
        json={"lease_id": reason_claim["lease_id"]},
    )
    assert committed.status_code == 200, committed.text
    return task, committed.json()["created_intent_ids"]


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
    assert stopped_intent["status"] == "cancelled"
    assert stopped_intent["last_failure_kind"] == "task_stopped"
    assert stopped_intent["worker"] is None
    assert stopped_intent["lease_id"] is None

    assert client.post(f"/api/v1/tasks/{task['id']}/resume").status_code == 200
    second_claim = client.post(endpoint, json={"worker_name": "worker-a"}).json()
    assert second_claim["lease_id"] != first_claim["lease_id"]


@pytest.mark.parametrize(
    ("runtime", "worker_capacity", "expected"),
    [
        (
            {
                "global_max_active_runs": 1,
                "global_max_active_tasks": 1,
                "per_task_max_active_runs": 1,
                "per_task_explore_concurrency": 1,
            },
            2,
            "Global active run limit reached",
        ),
        (
            {
                "global_max_active_runs": 2,
                "global_max_active_tasks": 1,
                "per_task_max_active_runs": 1,
                "per_task_explore_concurrency": 1,
            },
            2,
            "Global active Task limit reached",
        ),
        (
            {
                "global_max_active_runs": 2,
                "global_max_active_tasks": 2,
                "per_task_max_active_runs": 1,
                "per_task_explore_concurrency": 1,
            },
            1,
            "Worker has no free capacity",
        ),
    ],
)
def test_claim_transaction_enforces_global_task_and_worker_limits(
    client,
    runtime,
    worker_capacity,
    expected,
) -> None:
    update_runtime_settings(client, **runtime)
    project = client.post("/api/v1/projects", json={"title": "claim limits"}).json()
    first = _bootstrap_task(
        client,
        project_id=project["id"],
        title="first",
        worker_name="worker-a",
    )
    second = _bootstrap_task(
        client,
        project_id=project["id"],
        title="second",
        worker_name="worker-a",
    )
    client.put(
        "/api/v1/workers/worker-a",
        json={"capabilities": ["bootstrap"], "max_running": worker_capacity},
    )

    assert client.post(
        f"/api/v1/dispatch/tasks/{first['id']}/intents/i001/claim",
        json={"worker_name": "worker-a"},
    ).status_code == 200
    conflict = client.post(
        f"/api/v1/dispatch/tasks/{second['id']}/intents/i001/claim",
        json={"worker_name": "worker-a"},
    )

    assert conflict.status_code == 409
    assert conflict.json()["error"]["message"] == expected


@pytest.mark.parametrize(
    ("runtime", "expected"),
    [
        (
            {
                "global_max_active_runs": 2,
                "global_max_active_tasks": 1,
                "per_task_max_active_runs": 1,
                "per_task_explore_concurrency": 1,
            },
            "Per-Task active run limit reached",
        ),
        (
            {
                "global_max_active_runs": 2,
                "global_max_active_tasks": 1,
                "per_task_max_active_runs": 2,
                "per_task_explore_concurrency": 1,
            },
            "Per-Task explore concurrency limit reached",
        ),
    ],
)
def test_claim_transaction_enforces_per_task_and_explore_limits(
    client,
    runtime,
    expected,
) -> None:
    task, intent_ids = _task_with_two_pending_explores(client, **runtime)
    first = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/intents/{intent_ids[0]}/claim",
        json={"worker_name": "worker-a"},
    )
    assert first.status_code == 200, first.text

    conflict = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/intents/{intent_ids[1]}/claim",
        json={"worker_name": "worker-a"},
    )

    assert conflict.status_code == 409
    assert conflict.json()["error"]["message"] == expected
