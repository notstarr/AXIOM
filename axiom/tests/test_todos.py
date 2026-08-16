from __future__ import annotations


def task_with_sources(client) -> dict:
    project = client.post("/api/v1/projects", json={"title": "系统"}).json()
    return client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "测试",
            "origin": "a.example.com",
            "goal": "完成测试",
            "hints": ["优先验证授权"],
        },
    ).json()


def test_todo_is_single_level_and_status_can_be_updated(client) -> None:
    task = task_with_sources(client)
    created = client.post(
        f"/api/v1/tasks/{task['id']}/todos",
        json={
            "title": "验证对象级授权",
            "category": "authorization",
            "theme": "IDOR",
            "priority": 90,
            "sources": [{"type": "fact", "id": "origin"}],
        },
    )
    assert created.status_code == 201, created.text
    todo = created.json()
    assert todo["id"] == "td001"
    assert todo["status"] == "open"
    assert todo["sources"] == [{"type": "fact", "id": "origin"}]

    updated = client.patch(
        f"/api/v1/tasks/{task['id']}/todos/{todo['id']}",
        json={
            "status": "completed",
            "status_reason": "已由后续测试确认",
            "source_adds": [{"type": "hint", "id": "h001"}],
        },
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["status"] == "completed"
    assert body["completed_at"] is not None
    assert body["sources"] == [
        {"type": "fact", "id": "origin"},
        {"type": "hint", "id": "h001"},
    ]


def test_todo_rejects_unknown_sources_atomically(client) -> None:
    task = task_with_sources(client)
    response = client.post(
        f"/api/v1/tasks/{task['id']}/todos",
        json={
            "title": "无效来源",
            "category": "other",
            "sources": [{"type": "fact", "id": "f999"}],
        },
    )

    assert response.status_code == 409
    assert client.get(f"/api/v1/tasks/{task['id']}/todos").json() == []


def test_todo_requires_a_category_and_source() -> None:
    # This is intentionally a request-shape assertion rather than a database
    # assertion: malformed model output must be rejected before a transaction.
    from axiom.modules.todos.schemas import CreateTodo

    try:
        CreateTodo(title="x", category="other", sources=[])
    except ValueError as exc:
        assert "source" in str(exc).lower()
    else:
        raise AssertionError("expected source validation failure")
