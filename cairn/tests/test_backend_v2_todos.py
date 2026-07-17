from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from axiom.api.v1.router import router
from axiom.infrastructure.database.engine import create_sqlite_engine
from axiom.infrastructure.database.metadata import metadata
from axiom.modules.projects.schemas import CreateProject
from axiom.modules.projects.service import ProjectService
from axiom.modules.tasks.schemas import CreateTask
from axiom.modules.tasks.service import TaskService
from axiom.modules.todos.schemas import CreateTodo, TodoSource
from axiom.modules.todos.service import TodoService


def make_domain(tmp_path):
    engine = create_sqlite_engine(tmp_path / "axiom-v2.db")
    metadata.create_all(engine)
    project = ProjectService(engine).create(CreateProject(title="Demo"))
    task = TaskService(engine).create(
        CreateTask(project_id=project.id, title="Task", origin="example.com", goal="finish")
    )
    return engine, project, task


def test_human_can_create_list_and_update_todo(tmp_path) -> None:
    engine, _project, task = make_domain(tmp_path)
    service = TodoService(engine)
    todo = service.create(
        task.id,
        CreateTodo(
            title="Check JWT forgery",
            category="authentication",
            theme="jwt",
            priority=80,
            sources=[TodoSource(type="origin", id="origin")],
            actor="human",
        ),
    )

    assert todo.id == "td001"
    assert todo.status == "pending"
    assert todo.priority == 80
    assert todo.sources == [TodoSource(type="origin", id="origin")]
    assert service.list(task.id)[0].title == "Check JWT forgery"

    updated = service.update(
        task.id,
        todo.id,
        status="completed",
        status_reason="verified by human",
        priority=90,
        actor="human",
    )
    assert updated.status == "completed"
    assert updated.status_reason == "verified by human"
    assert updated.priority == 90
    assert updated.completed_at is not None


def test_todo_requires_valid_source(tmp_path) -> None:
    engine, _project, task = make_domain(tmp_path)
    service = TodoService(engine)

    with pytest.raises(ValueError, match="at least one source"):
        service.create(
            task.id,
            CreateTodo(
                title="No source",
                category="other",
                sources=[],
                actor="human",
            ),
        )

    with pytest.raises(ValueError, match="Todo source not found"):
        service.create(
            task.id,
            CreateTodo(
                title="Bad source",
                category="other",
                sources=[TodoSource(type="fact", id="f999")],
                actor="human",
            ),
        )


def test_todo_schema_rejects_invalid_category_and_priority() -> None:
    with pytest.raises(ValidationError):
        CreateTodo(
            title="bad",
            category="random",
            priority=101,
            sources=[TodoSource(type="origin", id="origin")],
            actor="human",
        )


def test_v1_todo_api(tmp_path) -> None:
    engine, project, task = make_domain(tmp_path)
    app = FastAPI()
    app.state.v2_engine = engine
    app.include_router(router)

    with TestClient(app) as client:
        created = client.post(
            f"/api/v1/tasks/{task.id}/todos",
            json={
                "title": "Test SSH login",
                "category": "network",
                "theme": "ssh",
                "priority": 70,
                "sources": [{"type": "origin", "id": "origin"}],
                "actor": "human",
            },
        )
        assert created.status_code == 201
        todo_id = created.json()["id"]

        listed = client.get(f"/api/v1/tasks/{task.id}/todos")
        assert listed.status_code == 200
        assert listed.json()[0]["id"] == todo_id

        updated = client.patch(
            f"/api/v1/tasks/{task.id}/todos/{todo_id}",
            json={"status": "active", "priority": 75, "actor": "human"},
        )
        assert updated.status_code == 200
        assert updated.json()["status"] == "active"
        assert updated.json()["priority"] == 75
