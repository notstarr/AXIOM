from __future__ import annotations


def create_project(client, title: str = "示例系统") -> dict:
    response = client.post("/api/v1/projects", json={"title": title})
    assert response.status_code == 201, response.text
    return response.json()


def test_task_creation_is_atomic_and_seeds_graph(client) -> None:
    project = create_project(client)
    response = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "授权边界测试",
            "origin": "https://API.Example.com:8443",
            "goal": "对该系统进行渗透测试，并挖掘10个高置信度的漏洞",
            "hints": ["优先验证对象级授权", "优先验证对象级授权"],
            "worker_pool": ["worker-a", "worker-a"],
        },
    )
    assert response.status_code == 201, response.text
    task = response.json()
    assert task["id"] == "t_001"
    assert task["origin"] == {
        "host": "api.example.com",
        "scheme": "https",
        "port": 8443,
        "original_input": "https://api.example.com:8443",
    }
    assert task["scope"] == {
        "enabled": True,
        "targets": ["api.example.com"],
        "exclude": [],
    }
    assert task["worker_pool"] == ["worker-a"]

    hints = client.get(f"/api/v1/tasks/{task['id']}/hints").json()
    assert [(item["id"], item["content"]) for item in hints] == [
        ("h001", "优先验证对象级授权")
    ]

    graph_response = client.get(f"/api/v1/tasks/{task['id']}/graph")
    assert graph_response.status_code == 200
    graph = graph_response.json()
    assert [fact["id"] for fact in graph["facts"]] == ["goal", "origin"]
    assert graph["intents"] == [
        {
            "id": "i001",
            "task_id": task["id"],
            "todo_id": None,
            "kind": "bootstrap",
            "from_facts": ["origin"],
            "description": "bootstrap",
            "status": "pending",
            "creator": "dispatcher.bootstrap",
            "worker": None,
            "worker_pool": ["worker-a"],
            "targets": ["api.example.com"],
            "execute_timeout_seconds": None,
            "conclude_timeout_seconds": None,
            "lease_id": None,
            "claimed_at": None,
            "last_heartbeat_at": None,
            "retry_count": 0,
            "infrastructure_retry_count": 0,
            "last_failure_kind": "",
            "failure_class": "",
            "activity_kind": "",
            "activity_summary": "",
            "activity_tool": "",
            "activity_started_at": None,
            "activity_updated_at": None,
            "next_retry_at": None,
            "to_fact_id": None,
            "created_at": graph["intents"][0]["created_at"],
            "concluded_at": None,
            "archived": False,
        }
    ]


def test_invalid_origin_does_not_leave_partial_task(client) -> None:
    project = create_project(client)
    response = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "非法起点",
            "origin": "https://example.com/private?token=secret",
            "goal": "测试目标",
        },
    )
    assert response.status_code == 422
    assert client.get("/api/v1/tasks").json() == []


def test_project_with_tasks_must_be_archived(client) -> None:
    project = create_project(client)
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "任务",
            "origin": "example.com",
            "goal": "完成测试",
        },
    ).json()

    response = client.delete(f"/api/v1/projects/{project['id']}")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"

    assert client.post(f"/api/v1/tasks/{task['id']}/stop").status_code == 200
    assert client.delete(f"/api/v1/tasks/{task['id']}").status_code == 204
    assert client.delete(f"/api/v1/projects/{project['id']}").status_code == 204


def test_scope_normalizes_exact_and_wildcard_targets(client) -> None:
    project = create_project(client)
    response = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "范围测试",
            "origin": "a.example.com",
            "goal": "测试授权边界",
            "scope": {
                "enabled": True,
                "targets": ["A.EXAMPLE.COM", "*.Example.com", "blocked.example.com"],
                "exclude": ["blocked.example.com"],
            },
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["scope"] == {
        "enabled": True,
        "targets": ["a.example.com", "*.example.com"],
        "exclude": ["blocked.example.com"],
    }


def test_task_settings_and_hints_can_be_edited_without_stale_graph_state(client) -> None:
    project = create_project(client)
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "原任务",
            "origin": "a.example.com",
        },
    ).json()
    assert task["goal"] == "对该系统进行渗透测试，并挖掘10个高置信度的漏洞"

    updated = client.patch(
        f"/api/v1/tasks/{task['id']}",
        json={
            "title": "新任务",
            "goal": "验证所有高价值攻击面",
            "scope": {"enabled": True, "targets": [], "exclude": ["blocked.example.com"]},
            "worker_pool": ["worker-b", "worker-b"],
            "guard_enabled": True,
            "external_report_enabled": True,
        },
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["title"] == "新任务"
    assert body["scope"]["targets"] == ["a.example.com"]
    assert body["worker_pool"] == ["worker-b"]
    assert body["guard_enabled"] is True
    assert body["external_report_enabled"] is True

    graph = client.get(f"/api/v1/tasks/{task['id']}/graph").json()
    goal = next(item for item in graph["facts"] if item["id"] == "goal")
    assert goal["description"] == "验证所有高价值攻击面"
    assert graph["intents"][0]["worker_pool"] == ["worker-b"]

    hint = client.post(
        f"/api/v1/tasks/{task['id']}/hints",
        json={"content": "登录入口位于 /admin", "creator": "operator"},
    )
    assert hint.status_code == 201, hint.text
    changed = client.patch(
        f"/api/v1/tasks/{task['id']}/hints/{hint.json()['id']}",
        json={"content": "登录入口位于 /console"},
    )
    assert changed.status_code == 200
    assert changed.json()["content"] == "登录入口位于 /console"

    archived = client.patch(
        f"/api/v1/tasks/{task['id']}/hints/{hint.json()['id']}",
        json={"archived": True},
    )
    assert archived.status_code == 200
    assert client.get(f"/api/v1/tasks/{task['id']}/hints").json() == []
