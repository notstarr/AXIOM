from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from axiom.api.v1.router import router
from axiom.infrastructure.database.engine import create_sqlite_engine
from axiom.infrastructure.database.metadata import metadata


def make_client(tmp_path) -> TestClient:
    app = FastAPI()
    app.state.v2_engine = create_sqlite_engine(tmp_path / "axiom-v2.db")
    metadata.create_all(app.state.v2_engine)
    app.include_router(router)
    return TestClient(app)


def test_v1_creates_and_reads_project_and_task(tmp_path) -> None:
    with make_client(tmp_path) as client:
        project_response = client.post(
            "/api/v1/projects",
            json={"title": "Demo", "description": "project"},
        )
        assert project_response.status_code == 201
        project = project_response.json()
        assert project["id"] == "prj_001"

        task_response = client.post(
            "/api/v1/tasks",
            json={
                "project_id": project["id"],
                "title": "Task",
                "origin": "https://api.example.com:8443",
                "goal": "confirm goal",
                "hints": ["initial clue"],
                "worker_pool": ["worker-a"],
            },
        )
        assert task_response.status_code == 201
        task = task_response.json()
        assert task["id"] == "t_001"
        assert task["origin"]["host"] == "api.example.com"
        assert task["scope"]["targets"] == ["api.example.com"]

        assert client.get("/api/v1/projects").json()[0]["id"] == project["id"]
        assert client.get("/api/v1/tasks").json()[0]["id"] == task["id"]
        assert client.get(f"/api/v1/tasks/{task['id']}").json()["phase"] == "bootstrap"


def test_v1_returns_clear_validation_and_not_found_errors(tmp_path) -> None:
    with make_client(tmp_path) as client:
        invalid = client.post(
            "/api/v1/tasks",
            json={
                "project_id": "prj_999",
                "title": "Task",
                "origin": "https://example.com/path",
                "goal": "finish",
            },
        )
        assert invalid.status_code == 422

        missing_project = client.post(
            "/api/v1/tasks",
            json={
                "project_id": "prj_999",
                "title": "Task",
                "origin": "example.com",
                "goal": "finish",
            },
        )
        assert missing_project.status_code == 404
        assert missing_project.json()["detail"] == "Project not found: prj_999"

        assert client.get("/api/v1/tasks/t_999").status_code == 404
