from __future__ import annotations

from pathlib import Path

import pytest

from axiom.infrastructure.database.engine import create_sqlite_engine
from axiom.infrastructure.database.metadata import metadata
from axiom.modules.artifacts.schemas import RegisterArtifact
from axiom.modules.artifacts.service import ArtifactService
from axiom.modules.projects.schemas import CreateProject
from axiom.modules.projects.service import ProjectService
from axiom.modules.tasks.schemas import CreateTask
from axiom.modules.tasks.service import TaskService


def test_artifact_registration_indexes_existing_file_without_copying(tmp_path) -> None:
    engine = create_sqlite_engine(tmp_path / "axiom-v2.db")
    metadata.create_all(engine)
    project = ProjectService(engine).create(CreateProject(title="Demo"))
    task = TaskService(engine).create(
        CreateTask(project_id=project.id, title="Task", origin="example.com", goal="finish")
    )
    data_dir = tmp_path / "data"
    artifact_path = data_dir / "projects" / project.id / task.id / "ai" / "i001_findings.md"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text("# findings\nconfirmed result\n", encoding="utf-8")

    service = ArtifactService(engine, data_dir=data_dir)
    artifact = service.register(
        task.id,
        RegisterArtifact(
            intent_id="i001",
            artifact_type="findings",
            path=str(artifact_path),
            summary="confirmed result",
            actor="bootstrap-worker",
        ),
    )

    assert artifact.id == "a001"
    assert artifact.path == str(artifact_path.resolve())
    assert artifact.size == artifact_path.stat().st_size
    assert len(artifact.content_hash) == 64
    assert service.list(task.id)[0].content_hash == artifact.content_hash

    artifact_path.write_text("# findings\nupdated evidence\n", encoding="utf-8")
    updated = service.register(
        task.id,
        RegisterArtifact(
            intent_id="i001",
            artifact_type="findings",
            path=str(artifact_path),
            summary="updated",
            actor="bootstrap-worker",
        ),
    )
    assert updated.id == artifact.id
    assert updated.content_hash != artifact.content_hash
    assert len(service.list(task.id)) == 1


def test_artifact_registration_rejects_files_outside_data_dir(tmp_path) -> None:
    engine = create_sqlite_engine(tmp_path / "axiom-v2.db")
    metadata.create_all(engine)
    project = ProjectService(engine).create(CreateProject(title="Demo"))
    task = TaskService(engine).create(
        CreateTask(project_id=project.id, title="Task", origin="example.com", goal="finish")
    )
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")

    with pytest.raises(ValueError, match="outside AXIOM_DATA_DIR"):
        ArtifactService(engine, data_dir=tmp_path / "data").register(
            task.id,
            RegisterArtifact(
                artifact_type="findings",
                path=str(outside),
                actor="worker",
            ),
        )
