from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from axiom.api.v1.router import router
from axiom.infrastructure.database.engine import create_sqlite_engine
from axiom.infrastructure.database.metadata import intents, metadata, reason_workspaces, todos
from axiom.modules.projects.schemas import CreateProject
from axiom.modules.projects.service import ProjectService
from axiom.modules.reason_workspace.schemas import DraftIntent, DraftTodo
from axiom.modules.reason_workspace.service import ReasonWorkspaceService
from axiom.modules.tasks.schemas import CreateTask
from axiom.modules.tasks.lifecycle import TaskLifecycleService
from axiom.modules.tasks.service import TaskService
from axiom.modules.todos.schemas import TodoSource


def make_workspace_service(tmp_path):
    engine = create_sqlite_engine(tmp_path / "axiom-v2.db")
    metadata.create_all(engine)
    project = ProjectService(engine).create(CreateProject(title="Demo"))
    task = TaskService(engine).create(
        CreateTask(project_id=project.id, title="Task", origin="api.example.com", goal="finish")
    )
    TaskLifecycleService(engine).claim_reason(task.id, worker="reasoner")
    return engine, task, ReasonWorkspaceService(engine)


def test_reason_workspace_commits_todos_and_intents_atomically(tmp_path) -> None:
    engine, task, service = make_workspace_service(tmp_path)
    workspace = service.open(task.id, worker="reasoner", context_revision=1)
    service.add_todo(
        workspace.id,
        DraftTodo(
            ref="t1",
            title="Verify JWT forgery",
            category="authentication",
            theme="jwt",
            priority=80,
            sources=[TodoSource(type="origin", id="origin")],
        ),
    )
    service.propose_intent(
        workspace.id,
        DraftIntent(
            todo_ref="t1",
            from_facts=["origin"],
            description="Verify JWT algorithm confusion",
            targets=["api.example.com"],
            execute_seconds=240,
            conclude_seconds=90,
        ),
    )

    result = service.commit(workspace.id)

    assert result.workspace.status == "committed"
    assert result.created_todo_ids == ["td001"]
    assert result.created_intent_ids == ["i002"]
    with engine.connect() as conn:
        todo = conn.execute(select(todos)).mappings().one()
        intent_rows = conn.execute(select(intents).order_by(intents.c.id)).mappings().all()
    assert todo["status"] == "active"
    assert intent_rows[1]["todo_id"] == "td001"
    assert json.loads(intent_rows[1]["targets_json"]) == ["api.example.com"]
    assert intent_rows[1]["execute_timeout_seconds"] == 240
    assert intent_rows[1]["conclude_timeout_seconds"] == 90


def test_reason_workspace_scope_failure_rolls_back_entire_commit(tmp_path) -> None:
    engine, task, service = make_workspace_service(tmp_path)
    workspace = service.open(task.id, worker="reasoner", context_revision=1)
    service.add_todo(
        workspace.id,
        DraftTodo(
            ref="t1",
            title="Out of scope idea",
            category="other",
            sources=[TodoSource(type="origin", id="origin")],
        ),
    )
    service.propose_intent(
        workspace.id,
        DraftIntent(
            todo_ref="t1",
            from_facts=["origin"],
            description="Do not dispatch this",
            targets=["other.example.com"],
            execute_seconds=240,
            conclude_seconds=90,
        ),
    )

    with pytest.raises(ValueError, match="outside Task Scope"):
        service.commit(workspace.id)

    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(todos)).scalar_one() == 0
        assert conn.execute(select(func.count()).select_from(intents)).scalar_one() == 1
        row = conn.execute(select(reason_workspaces)).mappings().one()
    assert row["status"] == "open"


def test_reason_workspace_can_only_commit_once(tmp_path) -> None:
    _engine, task, service = make_workspace_service(tmp_path)
    workspace = service.open(task.id, worker="reasoner", context_revision=1)
    service.commit(workspace.id)

    with pytest.raises(ValueError, match="already committed"):
        service.commit(workspace.id)


def test_reason_intent_requires_timeouts_and_one_todo_reference() -> None:
    with pytest.raises(Exception):
        DraftIntent(
            todo_ref="t1",
            todo_id="td001",
            from_facts=["origin"],
            description="invalid",
            targets=["api.example.com"],
            execute_seconds=0,
            conclude_seconds=90,
        )


def test_reason_workspace_v1_api_flow(tmp_path) -> None:
    engine, task, _service = make_workspace_service(tmp_path)
    app = FastAPI()
    app.state.v2_engine = engine
    app.state.service_token = "service-token"
    app.include_router(router)

    with TestClient(app) as client:
        assert client.post(
            f"/api/v1/tasks/{task.id}/reason/workspaces",
            json={"worker": "reasoner", "context_revision": 1},
        ).status_code == 401
        headers = {"Authorization": "Bearer service-token"}
        opened = client.post(
            f"/api/v1/tasks/{task.id}/reason/workspaces",
            json={"worker": "reasoner", "context_revision": 1},
            headers=headers,
        )
        assert opened.status_code == 201
        workspace_id = opened.json()["id"]
        assert client.post(
            f"/api/v1/reason-workspaces/{workspace_id}/todos",
            json={
                "ref": "t1",
                "title": "Verify JWT",
                "category": "authentication",
                "theme": "jwt",
                "priority": 80,
                "sources": [{"type": "origin", "id": "origin"}],
            },
            headers=headers,
        ).status_code == 200
        assert client.post(
            f"/api/v1/reason-workspaces/{workspace_id}/intents",
            json={
                "todo_ref": "t1",
                "from_facts": ["origin"],
                "description": "Verify JWT algorithm confusion",
                "targets": ["api.example.com"],
                "execute_seconds": 240,
                "conclude_seconds": 90,
            },
            headers=headers,
        ).status_code == 200
        committed = client.post(
            f"/api/v1/reason-workspaces/{workspace_id}/commit",
            headers=headers,
        )
        assert committed.status_code == 200
        assert committed.json()["created_todo_ids"] == ["td001"]
        assert committed.json()["created_intent_ids"] == ["i002"]
