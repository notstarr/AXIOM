from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from axiom.api.v1.router import router
from axiom.infrastructure.database.engine import create_sqlite_engine
from axiom.infrastructure.database.metadata import artifacts, metadata
from axiom.modules.common.time import utcnow
from axiom.modules.graph.service import IntentService
from axiom.modules.projects.schemas import CreateProject
from axiom.modules.projects.service import ProjectService
from axiom.modules.tasks.lifecycle import TaskLifecycleService
from axiom.modules.tasks.schemas import CreateTask
from axiom.modules.tasks.service import TaskService


def make_domain(tmp_path):
    engine = create_sqlite_engine(tmp_path / "axiom-v2.db")
    metadata.create_all(engine)
    project = ProjectService(engine).create(CreateProject(title="Demo"))
    task = TaskService(engine).create(
        CreateTask(project_id=project.id, title="Task", origin="example.com", goal="finish")
    )
    return engine, task


def register_bootstrap_artifacts(engine, task_id: str) -> None:
    now = utcnow()
    with engine.begin() as conn:
        conn.execute(
            artifacts.insert(),
            [
                {
                    "id": f"a{index:03d}",
                    "task_id": task_id,
                    "intent_id": "i001",
                    "artifact_type": artifact_type,
                    "path": f"/tmp/{task_id}-{artifact_type}.md",
                    "summary": artifact_type,
                    "content_hash": str(index) * 64,
                    "size": 1,
                    "created_by": "test",
                    "created_at": now,
                }
                for index, artifact_type in enumerate(
                    ["asset_inventory", "initial_leads", "findings"], start=1
                )
            ],
        )


def test_intent_claim_heartbeat_release_and_conclude(tmp_path) -> None:
    engine, task = make_domain(tmp_path)
    service = IntentService(engine)

    claimed = service.claim(task.id, "i001", worker="worker-a")
    assert claimed.status == "claimed"
    assert claimed.worker == "worker-a"

    running = service.heartbeat(task.id, "i001", worker="worker-a")
    assert running.status == "running"

    with pytest.raises(ValueError, match="claimed by worker-a"):
        service.heartbeat(task.id, "i001", worker="worker-b")

    released = service.release(task.id, "i001", worker="worker-a", failure_kind="unhealthy")
    assert released.status == "pending"
    assert released.worker is None
    assert released.retry_count == 1

    service.claim(task.id, "i001", worker="worker-b")
    with pytest.raises(ValueError, match="required artifacts"):
        service.conclude(task.id, "i001", worker="worker-b", description="not ready")
    register_bootstrap_artifacts(engine, task.id)
    result = service.conclude(
        task.id,
        "i001",
        worker="worker-b",
        description="Bootstrap summary with artifact indexes",
    )
    assert result.intent.status == "completed"
    assert result.fact.id == "f001"
    assert result.intent.to_fact_id == "f001"


def test_reason_lease_is_task_scoped_and_exclusive(tmp_path) -> None:
    engine, task = make_domain(tmp_path)
    lifecycle = TaskLifecycleService(engine)

    claimed = lifecycle.claim_reason(task.id, worker="reason-a")
    assert claimed.reason_worker == "reason-a"
    with pytest.raises(ValueError, match="claimed by reason-a"):
        lifecycle.claim_reason(task.id, worker="reason-b")
    assert lifecycle.heartbeat_reason(task.id, worker="reason-a").reason_worker == "reason-a"
    assert lifecycle.release_reason(task.id, worker="reason-a").reason_worker is None


def test_stop_resume_and_goal_completion_change_task_and_intents(tmp_path) -> None:
    engine, task = make_domain(tmp_path)
    intents = IntentService(engine)
    lifecycle = TaskLifecycleService(engine)
    intents.claim(task.id, "i001", worker="worker-a")

    stopped = lifecycle.stop(task.id)
    assert stopped.status == "stopped"
    assert intents.get(task.id, "i001").status == "pending"
    assert intents.get(task.id, "i001").worker is None

    resumed = lifecycle.resume(task.id)
    assert resumed.status == "running"
    assert resumed.phase == "bootstrap"

    completed = lifecycle.complete(task.id, description="Goal is proven")
    assert completed.status == "completed"
    assert completed.phase == "none"
    assert intents.get(task.id, "i001").status == "cancelled"


def test_internal_lifecycle_api_requires_token_and_runs_commands(tmp_path) -> None:
    engine, task = make_domain(tmp_path)
    app = FastAPI()
    app.state.v2_engine = engine
    app.state.service_token = "service-token"
    app.include_router(router)
    headers = {"Authorization": "Bearer service-token"}

    with TestClient(app) as client:
        path = f"/api/v1/internal/tasks/{task.id}/intents/i001/claim"
        assert client.post(path, json={"worker": "worker-a"}).status_code == 401
        claimed = client.post(path, json={"worker": "worker-a"}, headers=headers)
        assert claimed.status_code == 200
        assert claimed.json()["status"] == "claimed"
        register_bootstrap_artifacts(engine, task.id)
        concluded = client.post(
            f"/api/v1/internal/tasks/{task.id}/intents/i001/conclude",
            json={"worker": "worker-a", "description": "bootstrap done"},
            headers=headers,
        )
        assert concluded.status_code == 200
        assert concluded.json()["fact"]["id"] == "f001"
