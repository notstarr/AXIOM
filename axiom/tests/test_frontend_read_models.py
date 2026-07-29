from __future__ import annotations

from sqlalchemy import event


def _project(client, title: str = "前端性能") -> dict:
    response = client.post("/api/v1/projects", json={"title": title})
    assert response.status_code == 201, response.text
    return response.json()


def _task(client, project_id: str, index: int = 1) -> dict:
    response = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project_id,
            "title": f"任务 {index}",
            "origin": f"app-{index}.example.com",
            "goal": "验证前端分页与聚合查询",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_task_list_uses_a_fixed_number_of_selects(client, engine) -> None:
    project = _project(client)
    for index in range(1, 13):
        _task(client, project["id"], index)

    statements: list[str] = []

    def record_select(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", record_select)
    try:
        response = client.get("/api/v1/tasks")
    finally:
        event.remove(engine, "before_cursor_execute", record_select)

    assert response.status_code == 200, response.text
    assert len(response.json()) == 12
    assert len(statements) <= 3
    assert all(item["artifact_count"] == 0 for item in response.json())


def test_paginated_artifact_vulnerability_and_todo_endpoints(client) -> None:
    project = _project(client)
    task = _task(client, project["id"])

    for index in range(3):
        response = client.post(
            f"/api/v1/tasks/{task['id']}/artifacts/upload",
            files={"file": (f"proof-{index}.txt", f"evidence {index}".encode(), "text/plain")},
            data={"actor": "human", "summary": f"证据 {index}"},
        )
        assert response.status_code == 201, response.text

    artifact_page = client.get("/api/v1/artifacts?limit=2&offset=1&search=证据")
    assert artifact_page.status_code == 200, artifact_page.text
    assert artifact_page.headers["X-Total-Count"] == "3"
    assert len(artifact_page.json()) == 2
    legacy_artifacts = client.get(f"/api/v1/tasks/{task['id']}/artifacts")
    assert legacy_artifacts.headers["X-Total-Count"] == "3"
    assert len(legacy_artifacts.json()) == 3

    severities = ["high", "medium", "low"]
    for index, severity in enumerate(severities):
        response = client.post(
            f"/api/v1/tasks/{task['id']}/vulnerabilities",
            json={
                "title": f"授权问题 {index}",
                "type": "越权访问",
                "severity": severity,
                "description": "普通用户可以读取其他用户资源",
                "impact": "造成跨用户数据泄露",
                "affected_target": f"app.example.com/orders/{index}",
            },
        )
        assert response.status_code == 201, response.text

    vulnerability_page = client.get(
        f"/api/v1/vulnerabilities?task_id={task['id']}&search=授权&limit=2&offset=0"
    )
    assert vulnerability_page.status_code == 200, vulnerability_page.text
    assert vulnerability_page.headers["X-Total-Count"] == "3"
    assert len(vulnerability_page.json()) == 2

    for index, status in enumerate(["pending", "active", "completed"]):
        response = client.post(
            f"/api/v1/tasks/{task['id']}/todos",
            json={
                "title": f"验证授权分支 {index}",
                "category": "authorization",
                "status": status,
                "sources": [{"type": "fact", "id": "origin"}],
            },
        )
        assert response.status_code == 201, response.text

    todo_page = client.get(
        f"/api/v1/tasks/{task['id']}/todos?search=授权&limit=1&offset=1"
    )
    assert todo_page.status_code == 200, todo_page.text
    assert todo_page.headers["X-Total-Count"] == "3"
    assert len(todo_page.json()) == 1


def test_dashboard_summary_returns_compact_aggregates(client) -> None:
    project = _project(client)
    task = _task(client, project["id"])
    response = client.post(
        f"/api/v1/tasks/{task['id']}/vulnerabilities",
        json={
            "title": "高危越权",
            "type": "越权访问",
            "severity": "high",
            "description": "普通用户可以读取其他用户资源",
            "impact": "造成数据泄露",
        },
    )
    assert response.status_code == 201, response.text

    summary = client.get("/api/v1/dashboard/summary")
    assert summary.status_code == 200, summary.text
    body = summary.json()
    assert body["task_status_counts"]["running"] == 1
    assert body["vulnerability_severity_counts"]["high"] == 1
    assert body["pending_intent_count"] == 0
    assert len(body["recent_events"]) <= 8
