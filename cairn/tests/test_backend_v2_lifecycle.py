from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from axiom.api.v1.router import router
from axiom.infrastructure.database.engine import create_sqlite_engine
from axiom.infrastructure.database.metadata import artifacts, intent_sources, intents, metadata, tasks
from axiom.modules.common.time import utcnow
from axiom.modules.graph.service import IntentService
from axiom.modules.projects.schemas import CreateProject
from axiom.modules.projects.service import ProjectService
from axiom.modules.tasks.lifecycle import TaskLifecycleService
from axiom.modules.tasks.schemas import CreateTask
from axiom.modules.tasks.service import TaskService
from sqlalchemy import update
from axiom.modules.knowledge.schemas import CreateTaskHint
from axiom.modules.knowledge.service import KnowledgeService


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
    assert "Bootstrap summary with artifact indexes" not in result.fact.description
    assert "攻击面覆盖 [a001]: asset_inventory" in result.fact.description
    assert "重要线索 [a002]: initial_leads" in result.fact.description
    assert "确认结果 [a003]: findings" in result.fact.description
    assert "产物索引：" in result.fact.description
    assert "产物索引：a001=asset_inventory" in result.fact.description


def test_intent_release_separates_infrastructure_and_execution_retry_budgets(tmp_path) -> None:
    engine, task = make_domain(tmp_path)
    service = IntentService(engine)

    service.claim(task.id, "i001", worker="worker-a")
    transient = service.release(
        task.id,
        "i001",
        worker="worker-a",
        failure_kind="v2protocolerror",
        failure_class="transient",
        next_retry_at="2026-07-22T08:00:00Z",
    )
    assert transient.retry_count == 0
    assert transient.infrastructure_retry_count == 1
    assert transient.failure_class == "transient"

    claimed_again = service.claim(task.id, "i001", worker="worker-b")
    assert claimed_again.next_retry_at is None
    execution = service.release(
        task.id,
        "i001",
        worker="worker-b",
        failure_kind="timeout",
        failure_class="execution",
    )
    assert execution.retry_count == 1
    assert execution.infrastructure_retry_count == 1
    assert execution.failure_class == "execution"


def test_reason_lease_is_task_scoped_and_exclusive(tmp_path) -> None:
    engine, task = make_domain(tmp_path)
    lifecycle = TaskLifecycleService(engine)

    claimed = lifecycle.claim_reason(task.id, worker="reason-a")
    assert claimed.reason_worker == "reason-a"
    with pytest.raises(ValueError, match="claimed by reason-a"):
        lifecycle.claim_reason(task.id, worker="reason-b")
    assert lifecycle.heartbeat_reason(task.id, worker="reason-a").reason_worker == "reason-a"
    assert lifecycle.release_reason(task.id, worker="reason-a").reason_worker is None


def test_reason_activity_is_owner_scoped_and_available_from_task_detail(tmp_path) -> None:
    engine, task = make_domain(tmp_path)
    lifecycle = TaskLifecycleService(engine)
    claimed = lifecycle.claim_reason(task.id, worker="reason-a")
    assert claimed.reason_activity_kind == "starting"
    assert claimed.reason_activity_started_at

    activity = lifecycle.report_reason_activity(
        task.id,
        worker="reason-a",
        kind="updating_todos",
        summary="正在更新 Todo 并整理未覆盖的攻击面",
    )
    assert activity.reason_activity_kind == "updating_todos"
    assert activity.reason_activity_summary == "正在更新 Todo 并整理未覆盖的攻击面"
    assert activity.reason_activity_updated_at

    with pytest.raises(ValueError, match="claimed by reason-a"):
        lifecycle.report_reason_activity(
            task.id,
            worker="reason-b",
            kind="analyzing",
            summary="must be rejected",
        )

    app = FastAPI()
    app.state.v2_engine = engine
    app.state.service_token = "service-token"
    app.include_router(router)
    with TestClient(app) as client:
        response = client.get(f"/api/v1/tasks/{task.id}")
        assert response.status_code == 200
        assert response.json()["reason_activity_summary"] == "正在更新 Todo 并整理未覆盖的攻击面"


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


def test_intent_activity_is_owner_scoped_and_available_from_public_detail(tmp_path) -> None:
    engine, task = make_domain(tmp_path)
    service = IntentService(engine)
    claimed = service.claim(task.id, "i001", worker="worker-a")
    assert claimed.activity_kind == "starting"
    assert claimed.activity_started_at

    activity = service.report_activity(
        task.id,
        "i001",
        worker="worker-a",
        kind="tool_running",
        summary="正在扫描 example.com 的开放端口和服务",
        tool="bash",
    )
    assert activity.activity_kind == "tool_running"
    assert activity.activity_tool == "bash"
    assert activity.activity_summary == "正在扫描 example.com 的开放端口和服务"
    assert activity.activity_updated_at

    with pytest.raises(ValueError, match="claimed by worker-a"):
        service.report_activity(
            task.id,
            "i001",
            worker="worker-b",
            kind="tool_running",
            summary="must be rejected",
        )

    app = FastAPI()
    app.state.v2_engine = engine
    app.state.service_token = "service-token"
    app.include_router(router)
    with TestClient(app) as client:
        response = client.get(f"/api/v1/tasks/{task.id}/intents/i001")
        assert response.status_code == 200
        assert response.json()["activity_summary"] == "正在扫描 example.com 的开放端口和服务"


def test_intent_completion_keeps_explore_phase_while_sibling_is_open(tmp_path) -> None:
    engine, task = make_domain(tmp_path)
    now = utcnow()
    with engine.begin() as conn:
        conn.execute(
            update(intents)
            .where(intents.c.task_id == task.id, intents.c.id == "i001")
            .values(status="completed", concluded_at=now)
        )
        for intent_id in ("i002", "i003"):
            conn.execute(
                intents.insert().values(
                    id=intent_id,
                    task_id=task.id,
                    description=intent_id,
                    status="pending",
                    creator="reason",
                    worker_pool_json="[]",
                    targets_json='["example.com"]',
                    created_at=now,
                )
            )
            conn.execute(
                intent_sources.insert().values(
                    task_id=task.id,
                    intent_id=intent_id,
                    fact_id="origin",
                )
            )
        conn.execute(update(tasks).where(tasks.c.id == task.id).values(phase="explore"))
        conn.execute(
            artifacts.insert(),
            [
                {"id": "a010", "task_id": task.id, "intent_id": "i002", "artifact_type": "findings", "path": "/tmp/i002-findings.md", "summary": "", "content_hash": "a" * 64, "size": 1, "created_by": "test", "created_at": now},
                {"id": "a011", "task_id": task.id, "intent_id": "i002", "artifact_type": "leads", "path": "/tmp/i002-leads.md", "summary": "", "content_hash": "b" * 64, "size": 1, "created_by": "test", "created_at": now},
            ],
        )
    service = IntentService(engine)
    service.claim(task.id, "i002", worker="worker-a")
    result = service.conclude(
        task.id,
        "i002",
        worker="worker-a",
        description="本轮阶段性结果见 findings.md，后续线索见 leads.md。",
        summary_source="explore_artifact_summary",
    )
    assert result.fact.description.startswith("本轮阶段性结果")
    assert TaskService(engine).get(task.id).phase == "explore"


def test_new_hint_unblocks_bootstrap_waiting_for_human_input(tmp_path) -> None:
    engine, task = make_domain(tmp_path)
    intents_service = IntentService(engine)
    intents_service.claim(task.id, "i001", worker="worker-a")
    intents_service.release(task.id, "i001", worker="worker-a", failure_kind="needs_hint")
    assert intents_service.get(task.id, "i001").last_failure_kind == "needs_hint"
    KnowledgeService(engine).add_task_hint(
        task.id,
        CreateTaskHint(content="Use this additional origin clue", actor="human"),
    )
    bootstrap = intents_service.get(task.id, "i001")
    assert bootstrap.retry_count == 0
    assert bootstrap.infrastructure_retry_count == 0
    assert bootstrap.last_failure_kind == ""
    assert bootstrap.failure_class == ""


def test_stop_and_resume_rearms_bootstrap_after_needs_hint(tmp_path) -> None:
    engine, task = make_domain(tmp_path)
    intents_service = IntentService(engine)
    lifecycle = TaskLifecycleService(engine)
    intents_service.claim(task.id, "i001", worker="worker-a")
    intents_service.release(task.id, "i001", worker="worker-a", failure_kind="needs_hint")
    lifecycle.stop(task.id)

    resumed = lifecycle.resume(task.id)
    bootstrap = intents_service.get(task.id, "i001")

    assert resumed.status == "running"
    assert resumed.phase == "bootstrap"
    assert bootstrap.status == "pending"
    assert bootstrap.retry_count == 0
    assert bootstrap.infrastructure_retry_count == 0
    assert bootstrap.last_failure_kind == ""
    assert bootstrap.failure_class == ""
    assert bootstrap.next_retry_at is None


def test_graph_management_restores_old_frontend_edit_archive_and_delete(tmp_path) -> None:
    engine, task = make_domain(tmp_path)
    app = FastAPI()
    app.state.v2_engine = engine
    app.state.service_token = "service-token"
    app.include_router(router)

    with TestClient(app) as client:
        updated_origin = client.patch(
            f"/api/v1/tasks/{task.id}/facts/origin",
            json={"description": "updated origin", "actor": "pytest"},
        )
        assert updated_origin.status_code == 200
        assert updated_origin.json()["description"] == "updated origin"

        now = utcnow()
        with engine.begin() as conn:
            conn.execute(
                intents.insert().values(
                    id="i002",
                    task_id=task.id,
                    description="pending exploration",
                    status="pending",
                    creator="reason",
                    worker_pool_json="[]",
                    targets_json='["example.com"]',
                    created_at=now,
                )
            )
            conn.execute(
                intent_sources.insert().values(
                    task_id=task.id,
                    intent_id="i002",
                    fact_id="origin",
                )
            )

        changed_intent = client.patch(
            f"/api/v1/tasks/{task.id}/intents/i002",
            json={"description": "changed exploration", "actor": "pytest"},
        )
        assert changed_intent.status_code == 200
        assert changed_intent.json()["description"] == "changed exploration"
        archived_intent = client.post(
            f"/api/v1/tasks/{task.id}/intents/i002/toggle-archive",
            json={"actor": "pytest"},
        )
        assert archived_intent.status_code == 200
        assert archived_intent.json()["archived"] is True
        graph = client.get(f"/api/v1/tasks/{task.id}/graph").json()
        assert next(item for item in graph["intents"] if item["id"] == "i002")["archived"] is True
        assert client.post(
            f"/api/v1/tasks/{task.id}/intents/i002/toggle-archive",
            json={"actor": "pytest"},
        ).json()["archived"] is False
        assert client.delete(f"/api/v1/tasks/{task.id}/intents/i002?actor=pytest").status_code == 204

        service = IntentService(engine)
        service.claim(task.id, "i001", worker="worker-a")
        register_bootstrap_artifacts(engine, task.id)
        concluded = service.conclude(task.id, "i001", worker="worker-a", description="bootstrap done")
        fact_id = concluded.fact.id
        archived_fact = client.post(
            f"/api/v1/tasks/{task.id}/facts/{fact_id}/toggle-archive",
            json={"actor": "pytest"},
        )
        assert archived_fact.status_code == 200
        assert archived_fact.json()["archived"] is True
        graph = client.get(f"/api/v1/tasks/{task.id}/graph").json()
        assert next(item for item in graph["facts"] if item["id"] == fact_id)["archived"] is True
        assert client.delete(f"/api/v1/tasks/{task.id}/facts/{fact_id}?actor=pytest").status_code == 204
        reopened = service.get(task.id, "i001")
        assert reopened.status == "pending"
        assert reopened.to_fact_id is None
        assert TaskService(engine).get(task.id).phase == "bootstrap"

        events = client.get(f"/api/v1/tasks/{task.id}/audit-events?limit=100").json()
        event_types = {item["event_type"] for item in events}
        assert {"fact.updated", "intent.updated", "intent.archived", "intent.deleted", "fact.archived", "fact.deleted"} <= event_types
