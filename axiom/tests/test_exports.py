from __future__ import annotations


def _task(client):
    project = client.post("/api/v1/projects", json={"title": "导出项目"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={"project_id": project["id"], "title": "导出任务", "origin": "https://example.test"},
    ).json()
    return task["id"]


def test_task_overview_export_excludes_audit_events_and_includes_graph(client) -> None:
    task_id = _task(client)
    hint = client.post(f"/api/v1/tasks/{task_id}/hints", json={"content": "只测主站"})
    assert hint.status_code == 201

    response = client.get(f"/api/v1/tasks/{task_id}/export?format=yaml")
    assert response.status_code == 200, response.text
    assert "概况:" in response.text
    assert "facts:" in response.text
    assert "intents:" in response.text
    assert "只测主站" in response.text
    assert "audit_events" not in response.text


def test_task_timeline_and_vulnerability_exports_are_text(client) -> None:
    task_id = _task(client)
    timeline = client.get(f"/api/v1/tasks/{task_id}/export?format=timeline")
    assert timeline.status_code == 200
    assert "任务时间线" in timeline.text

    report = client.get(f"/api/v1/tasks/{task_id}/vulnerabilities-export")
    assert report.status_code == 200
    assert "当前没有漏洞记录" in report.text
