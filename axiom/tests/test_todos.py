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


def test_todo_counter_auto_aligns_when_counter_falls_behind(client) -> None:
    from axiom.modules.todos.tables import todos
    from axiom.shared.ids import id_counters
    from sqlalchemy import update

    task = task_with_sources(client)
    task_id = task["id"]

    # 正常创建 td001
    created1 = client.post(
        f"/api/v1/tasks/{task_id}/todos",
        json={
            "title": "第一个 Todo",
            "category": "authorization",
            "sources": [{"type": "fact", "id": "origin"}],
        },
    )
    assert created1.status_code == 201
    assert created1.json()["id"] == "td001"

    # 模拟外部/前序直接插入了 td015，而 id_counters 仍然停留在 2 (即下一次要分配 td002)
    container = client.app.state.container
    with container.engine.begin() as conn:
        conn.execute(
            todos.insert().values(
                id="td015",
                task_id=task_id,
                title="遗留 Todo 15",
                category="other",
                theme="legacy",
                priority=10,
                status="open",
                created_by="system",
                created_at="2026-08-23T00:00:00Z",
                updated_at="2026-08-23T00:00:00Z",
                archived=False,
            )
        )
        # 将计数器故意重设/倒退为 2
        conn.execute(
            update(id_counters)
            .where(id_counters.c.scope == task_id, id_counters.c.kind == "todo")
            .values(next_value=2)
        )

    # 再次创建 Todo，验证 _align_todo_counter 自动跳过冲突，分配出 td016
    created2 = client.post(
        f"/api/v1/tasks/{task_id}/todos",
        json={
            "title": "自愈后的新 Todo",
            "category": "authorization",
            "sources": [{"type": "fact", "id": "origin"}],
        },
    )
    assert created2.status_code == 201, created2.text
    assert created2.json()["id"] == "td016"
