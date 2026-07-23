from __future__ import annotations

from fastapi.testclient import TestClient

from axiom.infrastructure.database.engine import create_sqlite_engine
from axiom.infrastructure.database.metadata import metadata
from axiom.server.v2_app import create_v2_app


def test_knowledge_and_task_hints_are_available_through_v1(tmp_path) -> None:
    engine = create_sqlite_engine(tmp_path / "v2.db")
    metadata.create_all(engine)
    app = create_v2_app(engine=engine)
    with TestClient(app) as client:
        project = client.post("/api/v1/projects", json={"title": "Demo"}).json()
        task = client.post(
            "/api/v1/tasks",
            json={
                "project_id": project["id"],
                "title": "Task",
                "origin": "example.com",
                "goal": "finish",
                "hints": ["initial hint"],
            },
        ).json()
        knowledge = client.post(
            f"/api/v1/projects/{project['id']}/knowledge",
            json={"title": "Auth", "content": "JWT is used", "hint": "Inspect claims", "actor": "human"},
        )
        assert knowledge.status_code == 201
        added_hint = client.post(
            f"/api/v1/tasks/{task['id']}/hints",
            json={"content": "Focus on authorization", "actor": "human"},
        )
        assert added_hint.status_code == 201
        assert len(client.get(f"/api/v1/projects/{project['id']}/knowledge").json()) == 1
        hints = client.get(f"/api/v1/tasks/{task['id']}/hints").json()
        assert [item["content"] for item in hints] == ["initial hint", "Focus on authorization"]
        updated_hint = client.patch(
            f"/api/v1/tasks/{task['id']}/hints/{added_hint.json()['id']}",
            json={"content": "Focus on IDOR", "actor": "human.frontend"},
        )
        assert updated_hint.status_code == 200
        assert updated_hint.json()["content"] == "Focus on IDOR"
        assert updated_hint.json()["creator"] == "human.frontend"
