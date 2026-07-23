from __future__ import annotations

from fastapi.testclient import TestClient

from axiom.infrastructure.database.engine import create_sqlite_engine
from axiom.infrastructure.database.metadata import metadata
from axiom.server.v2_app import create_v2_app


def test_management_crud_search_status_and_exports(tmp_path) -> None:
    engine = create_sqlite_engine(tmp_path / "v2.db")
    metadata.create_all(engine)
    app = create_v2_app(
        engine=engine,
        data_dir=tmp_path / "data",
        service_token="service-token",
    )

    with TestClient(app) as client:
        project = client.post(
            "/api/v1/projects",
            json={"title": "Payments", "description": "API security"},
        ).json()
        archived = client.post("/api/v1/projects", json={"title": "Archive me"}).json()
        assert client.patch(
            f"/api/v1/projects/{archived['id']}", json={"status": "archived"}
        ).status_code == 200
        assert [item["id"] for item in client.get("/api/v1/projects?search=payment").json()] == [
            project["id"]
        ]
        assert [item["id"] for item in client.get("/api/v1/projects?status=archived").json()] == [
            archived["id"]
        ]

        task = client.post(
            "/api/v1/tasks",
            json={
                "project_id": project["id"],
                "title": "Authorization pass",
                "origin": "api.example.com",
                "goal": "find authorization flaws",
            },
        ).json()
        assert task["fact_count"] == 2 and task["intent_count"] == 1
        project_summary = client.get(f"/api/v1/projects/{project['id']}").json()
        assert project_summary["task_count"] == 1
        assert project_summary["active_task_count"] == 1
        assert client.get("/api/v1/tasks?search=authorization").json()[0]["id"] == task["id"]
        updated_task = client.patch(
            f"/api/v1/tasks/{task['id']}",
            json={"title": "Authorization deep pass", "worker_pool": ["worker-a"]},
        ).json()
        assert updated_task["worker_pool"] == ["worker-a"]
        scoped = client.patch(
            f"/api/v1/tasks/{task['id']}/scope",
            json={
                "enabled": True,
                "targets": ["api.example.com"],
                "exclude": ["admin.example.com"],
                "notes": "task-local",
            },
        ).json()
        assert scoped["scope"]["exclude"] == ["admin.example.com"]

        credential = client.post(
            f"/api/v1/projects/{project['id']}/credentials",
            json={
                "label": "user-cookie",
                "auth_type": "cookie",
                "data": {"Cookie": "sid=plain"},
                "domains": ["api.example.com"],
                "active": False,
            },
        ).json()
        assert client.get(f"/api/v1/projects/{project['id']}/credentials").json() == []
        all_credentials = client.get(
            f"/api/v1/projects/{project['id']}/credentials?include_inactive=true"
        ).json()
        assert all_credentials[0]["data"]["Cookie"] == "sid=plain"
        assert client.get(f"/api/v1/projects/{project['id']}").json()["credential_count"] == 1
        assert client.patch(
            f"/api/v1/projects/{project['id']}/credentials/{credential['id']}",
            json={"active": True},
        ).json()["active"] is True

        knowledge = client.post(
            f"/api/v1/projects/{project['id']}/knowledge",
            json={"title": "Auth", "content": "JWT", "actor": "human"},
        ).json()
        changed = client.patch(
            f"/api/v1/projects/{project['id']}/knowledge/{knowledge['id']}",
            json={"content": "Cookie", "actor": "human.frontend"},
        ).json()
        assert changed["content"] == "Cookie"
        assert client.get(f"/api/v1/projects/{project['id']}").json()["knowledge_count"] == 1
        assert client.delete(
            f"/api/v1/projects/{project['id']}/knowledge/{knowledge['id']}"
        ).status_code == 204

        vulnerability = client.post(
            f"/api/v1/tasks/{task['id']}/vulnerabilities",
            json={
                "title": "IDOR in invoice API",
                "type": "IDOR",
                "severity": "high",
                "description": "Invoice ownership is not checked",
                "impact": "Read another user's invoice",
                "status": "待验证",
                "source": "human",
                "reported_by": "tester",
            },
        ).json()
        assert client.get(f"/api/v1/projects/{project['id']}").json()[
            "vulnerability_count"
        ] == 1
        assert client.get("/api/v1/vulnerabilities?search=invoice&severity=high").json()[0][
            "id"
        ] == vulnerability["id"]
        changed_vulnerability = client.patch(
            f"/api/v1/tasks/{task['id']}/vulnerabilities/{vulnerability['id']}",
            json={"status": "已验证", "status_reason": "reproduced"},
        ).json()
        assert changed_vulnerability["status_reason"] == "reproduced"
        report = client.get(
            f"/api/v1/tasks/{task['id']}/vulnerabilities/{vulnerability['id']}/export"
        )
        assert report.status_code == 200 and "IDOR in invoice API" in report.text
        assert client.delete(
            f"/api/v1/tasks/{task['id']}/vulnerabilities/{vulnerability['id']}"
        ).status_code == 204
        assert client.get(f"/api/v1/tasks/{task['id']}/vulnerabilities").json() == []

        assert client.post(
            "/api/v1/internal/audit-events",
            headers={"Authorization": "Bearer service-token"},
            json={
                "project_id": project["id"],
                "task_id": task["id"],
                "intent_id": "i001",
                "worker": "worker-a",
                "event_type": "bootstrap.completed",
                "message": "Bootstrap completed for i001",
                "payload": {
                    "phase": "bootstrap",
                    "token_usage": {
                        "input_tokens": 100,
                        "output_tokens": 25,
                        "total_tokens": 125,
                    },
                },
            },
        ).status_code == 201
        assert client.post(
            "/api/v1/internal/audit-events",
            headers={"Authorization": "Bearer service-token"},
            json={
                "project_id": project["id"],
                "task_id": task["id"],
                "intent_id": "i001",
                "worker": "worker-a",
                "event_type": "guard.decision",
                "message": "Guard ALLOW: read",
                "payload": {
                    "command": "read /opt/axiom/dispatcher/skills/web-recon/SKILL.md",
                    "phase": "bootstrap",
                },
            },
        ).status_code == 201
        diagnostics = client.get(f"/api/v1/tasks/{task['id']}/diagnostics").json()
        assert diagnostics["health"]["status"] == "ok"
        assert diagnostics["worker_rankings"]["phases"] == [
            "bootstrap",
            "explore",
            "reason",
        ]
        assert "maintainer" not in diagnostics["worker_rankings"]["boards"]
        assert {row["phase"] for row in diagnostics["token_rankings"]["by_phase"]} == {
            "bootstrap",
            "explore",
            "reason",
        }
        assert diagnostics["worker_rankings"]["boards"]["bootstrap"]["success"] == 1
        assert diagnostics["token_rankings"]["totals"]["total_tokens"] == 125
        assert diagnostics["token_rankings"]["by_worker"][0]["worker"] == "worker-a"
        assert client.get("/api/v1/skills/usage").json() == {
            "items": [{"skill": "web-recon", "count": 1}]
        }
