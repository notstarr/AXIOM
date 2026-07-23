from __future__ import annotations


def test_diagnostics_uses_durable_lifecycle_events(client) -> None:
    worker = client.put(
        "/api/v1/workers/pi-main",
        json={"config": {"model": "test-model"}},
    )
    assert worker.status_code == 200
    project = client.post("/api/v1/projects", json={"title": "诊断项目"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "诊断任务",
            "origin": "example.test",
            "worker_pool": ["pi-main"],
        },
    ).json()
    task_id = task["id"]
    claimed = client.post(
        f"/api/v1/dispatch/tasks/{task_id}/intents/i001/claim",
        json={"worker_name": "pi-main"},
    ).json()
    lease = claimed["lease_id"]
    assert client.post(
        f"/api/v1/dispatch/tasks/{task_id}/intents/i001/start",
        json={"lease_id": lease},
    ).status_code == 200
    assert client.post(
        f"/api/v1/dispatch/tasks/{task_id}/intents/i001/release",
        json={
            "lease_id": lease,
            "failure_kind": "execute_timeout",
            "failure_class": "execution",
        },
    ).status_code == 200

    response = client.get(f"/api/v1/tasks/{task_id}/diagnostics")
    assert response.status_code == 200, response.text
    body = response.json()
    board = body["worker_rankings"]["boards"]["bootstrap"]
    assert board["total"] == 1
    assert board["timed_out"] == 1
    assert board["workers"][0]["worker"] == "pi-main"
    assert body["health"]["unclaimed_intent_count"] == 1
    assert body["config_status"]["is_current"] is True


def test_task_and_project_responses_include_live_counts(client) -> None:
    project = client.post("/api/v1/projects", json={"title": "统计项目"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={"project_id": project["id"], "title": "统计任务", "origin": "example.test"},
    ).json()

    task_view = client.get(f"/api/v1/tasks/{task['id']}").json()
    project_view = client.get(f"/api/v1/projects/{project['id']}").json()
    assert task_view["fact_count"] == 2
    assert task_view["intent_count"] == 1
    assert project_view["task_count"] == 1
    assert project_view["active_task_count"] == 1
