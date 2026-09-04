from __future__ import annotations


def _project_and_task(client) -> dict:
    project = client.post("/api/v1/projects", json={"title": "漏洞已阅测试"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "漏洞人工已阅",
            "origin": "reviewed.example.com",
        },
    ).json()
    return task


def _create_vuln(client, task: dict, title: str = "测试漏洞") -> dict:
    response = client.post(
        f"/api/v1/tasks/{task['id']}/vulnerabilities",
        json={
            "title": title,
            "type": "authorization",
            "severity": "high",
            "description": "测试描述",
            "impact": "测试影响",
            "affected_target": "reviewed.example.com",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_human_reviewed_toggle_roundtrip(client) -> None:
    task = _project_and_task(client)
    vuln = _create_vuln(client, task)
    assert vuln["human_reviewed"] is False
    assert vuln["human_reviewed_at"] is None
    assert vuln["human_reviewed_by"] is None

    marked = client.post(
        f"/api/v1/tasks/{task['id']}/vulnerabilities/{vuln['id']}/human-reviewed",
        json={"value": True, "actor": "hexin"},
    )
    assert marked.status_code == 200, marked.text
    body = marked.json()
    assert body["human_reviewed"] is True
    assert body["human_reviewed_by"] == "hexin"
    assert body["human_reviewed_at"]

    fetched = client.get(
        f"/api/v1/tasks/{task['id']}/vulnerabilities/{vuln['id']}"
    ).json()
    assert fetched["human_reviewed"] is True
    assert fetched["human_reviewed_by"] == "hexin"

    cleared = client.post(
        f"/api/v1/tasks/{task['id']}/vulnerabilities/{vuln['id']}/human-reviewed",
        json={"value": False},
    )
    assert cleared.status_code == 200, cleared.text
    body = cleared.json()
    assert body["human_reviewed"] is False
    assert body["human_reviewed_at"] is None
    assert body["human_reviewed_by"] is None


def test_human_reviewed_toggle_does_not_touch_status_or_reviews(client) -> None:
    task = _project_and_task(client)
    vuln = _create_vuln(client, task)
    client.patch(
        f"/api/v1/tasks/{task['id']}/vulnerabilities/{vuln['id']}",
        json={"status": "已验证"},
    )
    marked = client.post(
        f"/api/v1/tasks/{task['id']}/vulnerabilities/{vuln['id']}/human-reviewed",
        json={"value": True},
    )
    assert marked.status_code == 200, marked.text
    body = marked.json()
    assert body["status"] == "已验证"
    assert body["human_reviewed"] is True


def test_human_reviewed_toggle_rejects_missing_vulnerability(client) -> None:
    task = _project_and_task(client)
    response = client.post(
        f"/api/v1/tasks/{task['id']}/vulnerabilities/v999/human-reviewed",
        json={"value": True},
    )
    assert response.status_code == 404
