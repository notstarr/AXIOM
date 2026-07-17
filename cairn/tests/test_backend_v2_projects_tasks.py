from __future__ import annotations

import json

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from axiom.infrastructure.database.engine import create_sqlite_engine
from axiom.infrastructure.database.metadata import facts, intents, metadata, task_hints, tasks
from axiom.modules.projects.schemas import CreateProject
from axiom.modules.projects.service import ProjectService
from axiom.modules.tasks.origin import parse_origin
from axiom.modules.tasks.schemas import CreateTask, TaskScope
from axiom.modules.tasks.service import TaskService


def make_services(tmp_path):
    engine = create_sqlite_engine(tmp_path / "axiom-v2.db")
    metadata.create_all(engine)
    return engine, ProjectService(engine), TaskService(engine)


def test_project_and_task_creation_use_readable_ids_and_bootstrap_graph(tmp_path) -> None:
    engine, project_service, task_service = make_services(tmp_path)
    project = project_service.create(CreateProject(title="Demo", description="project"))
    second = project_service.create(CreateProject(title="Second"))

    assert project.id == "prj_001"
    assert second.id == "prj_002"

    task = task_service.create(
        CreateTask(
            project_id=project.id,
            title="API test",
            origin="https://API.Example.com:8443",
            goal="confirm the goal",
            hints=["use the supplied test account"],
            guard_enabled=True,
            external_report_enabled=True,
            worker_pool=["worker-a", "worker-b"],
        )
    )

    assert task.id == "t_001"
    assert task.origin.host == "api.example.com"
    assert task.origin.scheme == "https"
    assert task.origin.port == 8443
    assert task.status == "running"
    assert task.phase == "bootstrap"
    assert task.scope.targets == ["api.example.com"]

    with engine.connect() as conn:
        task_row = conn.execute(select(tasks).where(tasks.c.id == task.id)).mappings().one()
        fact_rows = conn.execute(select(facts).where(facts.c.task_id == task.id)).mappings().all()
        intent_rows = conn.execute(select(intents).where(intents.c.task_id == task.id)).mappings().all()
        hint_rows = conn.execute(select(task_hints).where(task_hints.c.task_id == task.id)).mappings().all()

    assert json.loads(task_row["scope_json"])["targets"] == ["api.example.com"]
    assert {row["id"] for row in fact_rows} == {"origin", "goal"}
    assert len(intent_rows) == 1
    assert intent_rows[0]["id"] == "i001"
    assert intent_rows[0]["creator"] == "dispatcher.bootstrap"
    assert intent_rows[0]["status"] == "pending"
    assert json.loads(intent_rows[0]["targets_json"]) == ["api.example.com"]
    assert [row["id"] for row in hint_rows] == ["h001"]


@pytest.mark.parametrize(
    "raw",
    [
        "https://example.com/path",
        "https://example.com?x=1",
        "https://user:pass@example.com",
        "*.example.com",
        "example.com\nother.example.com",
        "ftp://example.com",
        "",
    ],
)
def test_origin_rejects_non_single_host_inputs(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_origin(raw)


def test_task_requires_existing_project(tmp_path) -> None:
    _engine, _project_service, task_service = make_services(tmp_path)

    with pytest.raises(ValueError, match="Project not found"):
        task_service.create(
            CreateTask(
                project_id="prj_999",
                title="missing",
                origin="10.0.0.8",
                goal="finish",
            )
        )


def test_task_accepts_and_updates_task_level_scope(tmp_path) -> None:
    _engine, project_service, task_service = make_services(tmp_path)
    project = project_service.create(CreateProject(title="Demo"))
    task = task_service.create(
        CreateTask(
            project_id=project.id,
            title="Scoped",
            origin="api.example.com",
            goal="finish",
            scope=TaskScope(
                enabled=True,
                targets=["api.example.com", "*.example.com"],
                exclude=["admin.example.com"],
                notes="Task-local only",
            ),
        )
    )
    assert task.scope.targets == ["api.example.com", "*.example.com"]
    updated = task_service.update_scope(
        task.id,
        task.scope.model_copy(update={"enabled": False}),
    )
    assert updated.scope.enabled is False


def test_create_models_trim_and_validate_required_text() -> None:
    project = CreateProject(title="  Demo  ")
    task = CreateTask(project_id=" prj_001 ", title=" T ", origin=" example.com ", goal=" G ")

    assert project.title == "Demo"
    assert task.project_id == "prj_001"
    assert task.origin == "example.com"

    with pytest.raises(ValidationError):
        CreateProject(title="   ")
