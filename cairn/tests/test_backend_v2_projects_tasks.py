from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from axiom.infrastructure.database.engine import create_sqlite_engine
from axiom.infrastructure.database.metadata import (
    artifacts,
    audit_events,
    facts,
    id_counters,
    intents,
    metadata,
    task_hints,
    tasks,
    vulnerabilities,
)
from axiom.modules.artifacts.schemas import RegisterArtifact
from axiom.modules.artifacts.service import ArtifactService
from axiom.modules.projects.schemas import CreateProject
from axiom.modules.projects.service import ProjectService
from axiom.modules.tasks.origin import parse_origin
from axiom.modules.tasks.schemas import CreateTask, TaskScope
from axiom.modules.tasks.service import TaskService
from axiom.modules.vulnerabilities.schemas import ReportVulnerability
from axiom.modules.vulnerabilities.service import VulnerabilityService


def make_services(tmp_path):
    engine = create_sqlite_engine(tmp_path / "axiom-v2.db")
    metadata.create_all(engine)
    data_dir = tmp_path / "data"
    return engine, ProjectService(engine, data_dir=data_dir), TaskService(engine, data_dir=data_dir)


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
    assert task.fact_count == 2
    assert task.intent_count == 1
    assert task.hint_count == 1

    listed = task_service.list()
    assert listed[0].fact_count == 2
    assert listed[0].intent_count == 1
    assert listed[0].hint_count == 1

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
    engine, project_service, task_service = make_services(tmp_path)
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
    with engine.connect() as conn:
        row = conn.execute(select(tasks.c.scope_enabled).where(tasks.c.id == task.id)).one()
    assert row.scope_enabled is False


def test_task_can_be_created_with_scope_explicitly_disabled(tmp_path) -> None:
    engine, project_service, task_service = make_services(tmp_path)
    project = project_service.create(CreateProject(title="Demo"))
    task = task_service.create(
        CreateTask(
            project_id=project.id,
            title="Unscoped",
            origin="api.example.com",
            goal="finish",
            scope=TaskScope(enabled=False),
        )
    )

    assert task.scope.enabled is False
    assert task.scope.targets == []
    with engine.connect() as conn:
        row = conn.execute(select(tasks.c.scope_enabled).where(tasks.c.id == task.id)).one()
    assert row.scope_enabled is False


def test_create_models_trim_and_validate_required_text() -> None:
    project = CreateProject(title="  Demo  ")
    task = CreateTask(project_id=" prj_001 ", title=" T ", origin=" example.com ", goal=" G ")

    assert project.title == "Demo"
    assert task.project_id == "prj_001"
    assert task.origin == "example.com"

    with pytest.raises(ValidationError):
        CreateProject(title="   ")


def test_delete_task_removes_database_and_all_task_owned_files(tmp_path) -> None:
    engine, project_service, task_service = make_services(tmp_path)
    project = project_service.create(CreateProject(title="Delete me"))
    task = task_service.create(
        CreateTask(project_id=project.id, title="Disposable", origin="example.com", goal="finish")
    )
    data_dir = tmp_path / "data"
    owned = data_dir / "custom" / task.id / "evidence.txt"
    owned.parent.mkdir(parents=True)
    owned.write_text("evidence", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("must not be touched", encoding="utf-8")
    ArtifactService(engine, data_dir=data_dir).register(
        task.id,
        RegisterArtifact(artifact_type="evidence", path=str(owned), actor="pytest"),
    )
    # This row simulates a pre-existing/corrupted external artifact reference.
    with engine.begin() as conn:
        conn.execute(
            artifacts.insert().values(
                id="a_external",
                task_id=task.id,
                intent_id=None,
                artifact_type="external",
                path=str(outside),
                summary="external",
                content_hash="hash",
                size=outside.stat().st_size,
                created_by="pytest",
                created_at="2026-01-01T00:00:00Z",
                archived=False,
            )
        )
        conn.execute(
            audit_events.insert().values(
                id="evt_delete_me",
                task_id=task.id,
                project_id=project.id,
                intent_id=None,
                workspace_id=None,
                worker="pytest",
                event_type="test",
                level="info",
                message="delete me",
                payload_json="{}",
                created_at="2026-01-01T00:00:00Z",
            )
        )
    VulnerabilityService(engine).report(
        task.id,
        ReportVulnerability(
            title="Disposable finding",
            type="test",
            severity="low",
            description="only exists to verify cascade",
            impact="none",
            source="pytest",
            reported_by="pytest",
        ),
    )
    for relative in (
        Path("contexts/tasks") / task.id,
        Path("contexts/attempts") / task.id,
        Path("runs") / task.id,
        Path("tasks") / task.id,
        Path("projects") / project.id / task.id,
    ):
        path = data_dir / relative
        path.mkdir(parents=True, exist_ok=True)
        (path / "marker.txt").write_text("owned", encoding="utf-8")
    manifest = data_dir / "contexts/manifests/reason-delete-me.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"task_id": task.id}), encoding="utf-8")

    task_service.delete(task.id)

    assert task_service.get(task.id) is None
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(vulnerabilities).where(vulnerabilities.c.task_id == task.id)).scalar_one() == 0
        assert conn.execute(select(func.count()).select_from(artifacts).where(artifacts.c.task_id == task.id)).scalar_one() == 0
        assert conn.execute(select(func.count()).select_from(audit_events).where(audit_events.c.task_id == task.id)).scalar_one() == 0
        assert conn.execute(select(func.count()).select_from(id_counters).where(id_counters.c.scope == task.id)).scalar_one() == 0
    assert not (data_dir / "contexts/tasks" / task.id).exists()
    assert not (data_dir / "contexts/attempts" / task.id).exists()
    assert not (data_dir / "runs" / task.id).exists()
    assert not (data_dir / "tasks" / task.id).exists()
    assert not (data_dir / "projects" / project.id / task.id).exists()
    assert not manifest.exists()
    assert outside.read_text(encoding="utf-8") == "must not be touched"


def test_orphan_task_storage_repair_removes_only_non_live_task_data(tmp_path) -> None:
    from axiom.modules.tasks.storage import TaskStorage

    data_dir = tmp_path / "data"
    for task_id in ("t_live", "t_deleted"):
        (data_dir / "runs" / task_id).mkdir(parents=True)
        (data_dir / "runs" / task_id / "marker").write_text("x", encoding="utf-8")
    manifests = data_dir / "contexts/manifests"
    manifests.mkdir(parents=True)
    (manifests / "live.json").write_text(json.dumps({"task_id": "t_live"}), encoding="utf-8")
    (manifests / "deleted.json").write_text(json.dumps({"task_id": "t_deleted"}), encoding="utf-8")

    TaskStorage(data_dir).delete_orphaned_task_data({"t_live"})

    assert (data_dir / "runs/t_live").exists()
    assert not (data_dir / "runs/t_deleted").exists()
    assert (manifests / "live.json").exists()
    assert not (manifests / "deleted.json").exists()


def test_delete_project_cleans_every_task_before_database_cascade(tmp_path) -> None:
    engine, project_service, task_service = make_services(tmp_path)
    project = project_service.create(CreateProject(title="Whole project"))
    first = task_service.create(
        CreateTask(project_id=project.id, title="First", origin="one.example.com", goal="finish")
    )
    second = task_service.create(
        CreateTask(project_id=project.id, title="Second", origin="two.example.com", goal="finish")
    )
    data_dir = tmp_path / "data"
    for task in (first, second):
        path = data_dir / "runs" / task.id
        path.mkdir(parents=True)
        (path / "result.txt").write_text("owned", encoding="utf-8")
    project_root = data_dir / "projects" / project.id
    (project_root / "shared").mkdir(parents=True)
    (project_root / "shared/info.txt").write_text("project owned", encoding="utf-8")

    project_service.delete(project.id)

    assert project_service.get(project.id) is None
    assert task_service.get(first.id) is None
    assert task_service.get(second.id) is None
    assert not (data_dir / "runs" / first.id).exists()
    assert not (data_dir / "runs" / second.id).exists()
    assert not project_root.exists()
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(id_counters).where(id_counters.c.scope.in_([first.id, second.id, project.id]))).scalar_one() == 0
        assert conn.execute(select(func.count()).select_from(audit_events).where(audit_events.c.project_id == project.id)).scalar_one() == 0
