from __future__ import annotations

from fastapi.testclient import TestClient

from axiom.infrastructure.database.engine import create_sqlite_engine
from axiom.infrastructure.database.metadata import metadata
from axiom.server.v2_app import create_v2_app


class SuccessfulMozzieClient:
    def __init__(self):
        self.reports = []

    def resolve_project(self, backend_domain: str) -> str:
        return "api-project"

    def report(self, payload):
        self.reports.append(payload)
        return {"success": True, "external_id": "moz-1"}


def test_external_report_preview_submit_and_attempt_index(tmp_path) -> None:
    engine = create_sqlite_engine(tmp_path / "v2.db")
    metadata.create_all(engine)
    app = create_v2_app(engine=engine, service_token="test-token")
    mozzie = SuccessfulMozzieClient()
    app.state.mozzie_client = mozzie
    headers = {"Authorization": "Bearer test-token"}
    with TestClient(app) as client:
        project = client.post("/api/v1/projects", json={"title": "Demo"}).json()
        task = client.post(
            "/api/v1/tasks",
            json={
                "project_id": project["id"],
                "title": "Task",
                "origin": "api.example.com",
                "goal": "finish",
                "external_report_enabled": True,
            },
        ).json()
        report = client.post(
            f"/api/v1/internal/tasks/{task['id']}/vulnerabilities",
            headers=headers,
            json={
                "intent_id": "i001",
                "title": "GET /users/{id} 越权访问",
                "type": "越权访问",
                "severity": "high",
                "description": "Cross-user read",
                "impact": "Private data disclosure",
                "reproduction_steps": ["Request another ID"],
                "affected_target": "api.example.com",
                "source": "explore",
                "reported_by": "worker-a",
            },
        )
        assert report.status_code == 201
        vulnerability = report.json()
        assert len(mozzie.reports) == 1

        preview = client.post(
            f"/api/v1/tasks/{task['id']}/vulnerabilities/{vulnerability['id']}/external-preview",
            json={},
        )
        assert preview.status_code == 200
        assert preview.json()["project"] == "api-project"

        submitted = client.post(
            f"/api/v1/tasks/{task['id']}/vulnerabilities/{vulnerability['id']}/external-report",
            json={},
        )
        assert submitted.status_code == 201
        assert submitted.json()["status"] == "success"
        attempts = client.get(f"/api/v1/tasks/{task['id']}/external-report-attempts").json()
        assert attempts[0]["response"]["external_id"] == "moz-1"
        assert len(attempts) == 2
