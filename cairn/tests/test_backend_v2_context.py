from __future__ import annotations

import json
from pathlib import Path

from axiom.dispatcher.context.builder import ContextBuilder
from axiom.infrastructure.database.engine import create_sqlite_engine
from axiom.infrastructure.database.metadata import metadata
from axiom.modules.knowledge.schemas import CreateKnowledge, CreateTaskHint
from axiom.modules.knowledge.service import KnowledgeService
from axiom.modules.projects.schemas import CreateProject
from axiom.modules.projects.service import ProjectService
from axiom.modules.tasks.schemas import CreateTask
from axiom.modules.tasks.service import TaskService


def test_context_builder_reuses_unchanged_resources_and_revisions(tmp_path) -> None:
    engine = create_sqlite_engine(tmp_path / "axiom-v2.db")
    metadata.create_all(engine)
    project = ProjectService(engine).create(CreateProject(title="Demo"))
    task = TaskService(engine).create(
        CreateTask(project_id=project.id, title="Task", origin="example.com", goal="finish")
    )
    knowledge = KnowledgeService(engine)
    knowledge.add_project_knowledge(
        project.id,
        CreateKnowledge(title="Auth", content="Uses JWT", actor="human"),
    )
    data_dir = tmp_path / "data"
    builder = ContextBuilder(engine, data_dir=data_dir)

    first = builder.build(task.id, attempt_id="reason-1")
    second = builder.build(task.id, attempt_id="reason-2")

    assert first.revision == 1
    assert second.revision == 1
    assert first.resources == second.resources
    assert Path(first.manifest_path).is_file()
    assert Path(second.manifest_path).is_file()

    knowledge.add_task_hint(
        task.id,
        CreateTaskHint(content="Prioritize authorization", actor="human"),
    )
    third = builder.build(task.id, attempt_id="reason-3")
    assert third.revision == 2
    assert third.resources["knowledge"] == first.resources["knowledge"]
    assert third.resources["hints"] != first.resources["hints"]
    manifest = json.loads(Path(third.manifest_path).read_text())
    assert manifest["task_id"] == task.id
    assert manifest["revision"] == 2
