from __future__ import annotations

from sqlalchemy import event

from axiom.modules.artifacts.tables import artifacts
from axiom.modules.graph.tables import intents


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

    for index, status in enumerate(["open", "partial", "completed"]):
        response = client.post(
            f"/api/v1/tasks/{task['id']}/todos",
            json={
                "title": f"验证授权分支 {index}",
                "category": "authorization",
                "status": status,
                "status_reason": "本轮状态说明" if status != "open" else "",
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


def test_artifact_folder_read_model_and_intent_filters(client, engine) -> None:
    project = _project(client)
    task = _task(client, project["id"])
    task_id = task["id"]
    upload = client.post(
        f"/api/v1/tasks/{task_id}/artifacts/upload",
        files={"file": ("operator-note.txt", b"manual note", "text/plain")},
        data={"actor": "human", "summary": "人工任务文件"},
    )
    assert upload.status_code == 201, upload.text

    with engine.begin() as conn:
        conn.execute(
            intents.update()
            .where(intents.c.task_id == task_id, intents.c.id == "i001")
            .values(
                description="建立资产清单",
                status="completed",
                created_at="2026-07-29T01:00:00Z",
            )
        )
        conn.execute(
            intents.insert(),
            [
                {
                    "id": "i002",
                    "task_id": task_id,
                    "kind": "explore",
                    "description": "验证跨组织越权访问",
                    "status": "completed",
                    "creator": "reason",
                    "created_at": "2026-07-29T02:00:00Z",
                },
            ],
        )
        conn.execute(
            artifacts.insert(),
            [
                {
                    "id": "a101",
                    "task_id": task_id,
                    "intent_id": "i001",
                    "artifact_type": "bootstrap.findings",
                    "storage_key": f"tasks/{task_id}/artifacts/i001/findings.md",
                    "summary": "Bootstrap findings",
                    "content_hash": "hash-a101",
                    "size": 100,
                    "created_by": "worker",
                    "created_at": "2026-07-29T01:10:00Z",
                    "archived": False,
                },
                {
                    "id": "a102",
                    "task_id": task_id,
                    "intent_id": "i002",
                    "artifact_type": "explore.findings",
                    "storage_key": f"tasks/{task_id}/artifacts/i002/findings.md",
                    "summary": "越权验证结果",
                    "content_hash": "hash-a102",
                    "size": 200,
                    "created_by": "worker",
                    "created_at": "2026-07-29T02:10:00Z",
                    "archived": False,
                },
                {
                    "id": "a103",
                    "task_id": task_id,
                    "intent_id": "i002",
                    "artifact_type": "explore.leads",
                    "storage_key": f"tasks/{task_id}/artifacts/i002/leads.md",
                    "summary": "待继续验证",
                    "content_hash": "hash-a103",
                    "size": 50,
                    "created_by": "worker",
                    "created_at": "2026-07-29T02:11:00Z",
                    "archived": False,
                },
                {
                    "id": "a104",
                    "task_id": task_id,
                    "intent_id": "i002",
                    "artifact_type": "explore.evidence",
                    "storage_key": f"tasks/{task_id}/artifacts/i002/evidence/old.txt",
                    "summary": "已归档证据",
                    "content_hash": "hash-a104",
                    "size": 999,
                    "created_by": "worker",
                    "created_at": "2026-07-29T02:12:00Z",
                    "archived": True,
                },
            ],
        )

    folders = client.get(f"/api/v1/tasks/{task_id}/artifact-folders?limit=60")
    assert folders.status_code == 200, folders.text
    assert folders.headers["X-Total-Count"] == "3"
    body = folders.json()
    assert [item["folder_key"] for item in body] == ["task", "i002", "i001"]
    assert body[1]["artifact_count"] == 2
    assert body[1]["total_size"] == 250
    assert body[1]["intent_description"] == "验证跨组织越权访问"

    searched = client.get(
        f"/api/v1/tasks/{task_id}/artifact-folders?search=越权&limit=60"
    )
    assert searched.headers["X-Total-Count"] == "1"
    assert searched.json()[0]["folder_key"] == "i002"

    intent_page = client.get(
        f"/api/v1/tasks/{task_id}/artifacts?intent_id=i002&limit=50"
    )
    assert intent_page.headers["X-Total-Count"] == "2"
    assert {item["id"] for item in intent_page.json()} == {"a102", "a103"}

    task_files = client.get(
        f"/api/v1/tasks/{task_id}/artifacts?unscoped_only=true&limit=50"
    )
    assert task_files.headers["X-Total-Count"] == "1"
    assert task_files.json()[0]["id"] == upload.json()["id"]

    conflicting = client.get(
        f"/api/v1/tasks/{task_id}/artifacts?intent_id=i002&unscoped_only=true&limit=50"
    )
    assert conflicting.status_code == 409

    artifact = client.get(f"/api/v1/tasks/{task_id}/artifacts/a102")
    assert artifact.status_code == 200, artifact.text
    assert artifact.json()["intent_id"] == "i002"


def test_artifact_folder_read_model_has_fixed_query_count(client, engine) -> None:
    project = _project(client)
    task = _task(client, project["id"])
    for index in range(4):
        response = client.post(
            f"/api/v1/tasks/{task['id']}/artifacts/upload",
            files={"file": (f"note-{index}.txt", b"note", "text/plain")},
        )
        assert response.status_code == 201, response.text

    statements: list[str] = []

    def record_select(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", record_select)
    try:
        response = client.get(
            f"/api/v1/tasks/{task['id']}/artifact-folders?limit=60"
        )
    finally:
        event.remove(engine, "before_cursor_execute", record_select)

    assert response.status_code == 200, response.text
    assert len(statements) <= 3
