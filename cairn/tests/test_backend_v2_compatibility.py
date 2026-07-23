from __future__ import annotations

import yaml
from fastapi.testclient import TestClient

from axiom.infrastructure.database.engine import create_sqlite_engine
from axiom.infrastructure.database.metadata import metadata
from axiom.server.v2_app import create_v2_app


def make_client(tmp_path) -> TestClient:
    engine = create_sqlite_engine(tmp_path / "axiom-v2.db")
    metadata.create_all(engine)
    app = create_v2_app(
        engine=engine,
        data_dir=tmp_path / "data",
        service_token="service-token",
    )
    return TestClient(app)


def create_task(client: TestClient) -> dict:
    project = client.post("/api/v1/projects", json={"title": "Compatibility"}).json()
    response = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "Old frontend contract",
            "origin": "api.example.com",
            "goal": "verify the goal",
            "hints": ["initial hint"],
            "worker_pool": ["worker-a"],
        },
    )
    assert response.status_code == 201
    return response.json()


def test_worker_pool_update_propagates_to_unclaimed_intents(tmp_path) -> None:
    with make_client(tmp_path) as client:
        task = create_task(client)
        response = client.patch(
            f"/api/v1/tasks/{task['id']}/worker-pool",
            json={"worker_pool": ["worker-b", "worker-b", " worker-c "]},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["task"]["worker_pool"] == ["worker-b", "worker-c"]
        assert payload["updated_unclaimed_intents"] == 1
        intents = client.get(f"/api/v1/tasks/{task['id']}/intents").json()
        assert intents[0]["worker_pool"] == ["worker-b", "worker-c"]


def test_task_export_restores_yaml_and_timeline_contract(tmp_path) -> None:
    with make_client(tmp_path) as client:
        task = create_task(client)
        headers = {"Authorization": "Bearer service-token"}
        event = client.post(
            "/api/v1/internal/audit-events",
            headers=headers,
            json={
                "project_id": task["project_id"],
                "task_id": task["id"],
                "event_type": "bootstrap.started",
                "message": "Bootstrap started",
            },
        )
        assert event.status_code == 201

        yaml_response = client.get(f"/api/v1/tasks/{task['id']}/export?format=yaml")
        assert yaml_response.status_code == 200
        payload = yaml.safe_load(yaml_response.text)
        assert payload["task"]["id"] == task["id"]
        assert payload["task"]["origin"] == "api.example.com"
        assert payload["hints"][0]["content"] == "initial hint"
        assert payload["intents"][0]["id"] == "i001"

        timeline = client.get(f"/api/v1/tasks/{task['id']}/export?format=timeline")
        assert timeline.status_code == 200
        assert "TASK CREATED" in timeline.text
        assert "bootstrap.started" in timeline.text
        assert "Bootstrap started" in timeline.text


def test_audit_events_support_incremental_old_frontend_polling(tmp_path) -> None:
    with make_client(tmp_path) as client:
        task = create_task(client)
        headers = {"Authorization": "Bearer service-token"}
        created = []
        for index in range(3):
            response = client.post(
                "/api/v1/internal/audit-events",
                headers=headers,
                json={
                    "project_id": task["project_id"],
                    "task_id": task["id"],
                    "event_type": "test.event",
                    "message": f"event-{index}",
                },
            )
            assert response.status_code == 201
            created.append(response.json())

        initial = client.get(
            f"/api/v1/tasks/{task['id']}/audit-events?after_id=0&limit=2"
        ).json()
        assert [item["message"] for item in initial] == ["event-0", "event-1"]

        incremental = client.get(
            f"/api/v1/tasks/{task['id']}/audit-events?after_id={initial[-1]['id']}&limit=2"
        ).json()
        assert [item["message"] for item in incremental] == ["event-2"]
        assert incremental[0]["id"] == created[-1]["id"]
