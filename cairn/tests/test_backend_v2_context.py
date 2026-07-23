from __future__ import annotations

import json
from pathlib import Path

import yaml
from fastapi.testclient import TestClient
from sqlalchemy import update

from axiom.dispatcher.context.builder import ContextBuilder
from axiom.dispatcher.context.reader import ContextReader
from axiom.infrastructure.database.engine import create_sqlite_engine
from axiom.infrastructure.database.metadata import (
    facts,
    intent_sources,
    intents,
    metadata,
    reason_workspaces,
    todos,
)
from axiom.modules.knowledge.schemas import CreateKnowledge, CreateTaskHint
from axiom.modules.knowledge.service import KnowledgeService
from axiom.modules.projects.schemas import CreateProject
from axiom.modules.projects.service import ProjectService
from axiom.modules.tasks.schemas import CreateTask
from axiom.modules.tasks.service import TaskService
from axiom.server.v2_app import create_v2_app


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
    assert {
        key: value for key, value in first.resources.items() if key != "phase_context"
    } == {
        key: value for key, value in second.resources.items() if key != "phase_context"
    }
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


def test_context_uses_compact_graph_index_and_expands_full_entities(tmp_path) -> None:
    engine = create_sqlite_engine(tmp_path / "axiom-v2.db")
    metadata.create_all(engine)
    project = ProjectService(engine).create(CreateProject(title="Demo"))
    task = TaskService(engine).create(
        CreateTask(project_id=project.id, title="Task", origin="example.com", goal="finish")
    )
    long_fact = "A" * 160
    long_intent = "B" * 180
    with engine.begin() as conn:
        conn.execute(
            facts.insert().values(
                id="f001",
                task_id=task.id,
                description=long_fact,
                created_at="2026-07-20T00:00:01Z",
            )
        )
        conn.execute(
            todos.insert().values(
                id="td001",
                task_id=task.id,
                title="Check authorization",
                category="authorization",
                theme="orders",
                priority=80,
                status="active",
                created_by="reasoner",
                status_reason="",
                created_at="2026-07-20T00:00:02Z",
                updated_at="2026-07-20T00:00:02Z",
            )
        )
        conn.execute(
            intents.insert().values(
                id="i002",
                task_id=task.id,
                todo_id="td001",
                description=long_intent,
                status="pending",
                creator="reasoner",
                targets_json='["example.com"]',
                created_at="2026-07-20T00:00:03Z",
            )
        )
        conn.execute(
            intent_sources.insert().values(
                task_id=task.id,
                intent_id="i002",
                fact_id="f001",
            )
        )

    snapshot = ContextBuilder(engine, data_dir=tmp_path / "data", preview_chars=40).build(
        task.id,
        attempt_id="explore-1",
        phase="explore",
        intent_id="i002",
    )
    graph = yaml.safe_load(Path(snapshot.resources["graph"]).read_text())
    fact_index = next(item for item in graph["facts"] if item["id"] == "f001")
    intent_index = next(item for item in graph["intents"] if item["id"] == "i002")
    assert fact_index["description"] == "A" * 40 + "…"
    assert intent_index["description"] == "B" * 40 + "…"
    assert long_fact not in Path(snapshot.resources["graph"]).read_text()
    assert long_intent not in Path(snapshot.resources["graph"]).read_text()

    phase_context = json.loads(Path(snapshot.resources["phase_context"]).read_text())
    packet = phase_context["execution_packet"]
    assert packet["intent"]["description"] == long_intent
    assert packet["facts"][0]["description"] == long_fact
    assert packet["todo"]["id"] == "td001"

    expanded = ContextReader(engine).expand(
        task.id,
        {"facts": ["f001"], "intents": ["i002"]},
    )
    assert expanded["facts"][0]["description"] == long_fact
    assert expanded["intents"][0]["description"] == long_intent

    with engine.begin() as conn:
        conn.execute(
            update(facts)
            .where(facts.c.task_id == task.id, facts.c.id == "f001")
            .values(description="A" * 159 + "C")
        )
    changed = ContextBuilder(engine, data_dir=tmp_path / "data", preview_chars=40).build(
        task.id,
        attempt_id="generic-after-hidden-change",
    )
    assert changed.revision == snapshot.revision + 1
    assert changed.resources["graph"] == snapshot.resources["graph"]


def test_internal_context_api_builds_phase_packet_and_expands_ids(tmp_path) -> None:
    engine = create_sqlite_engine(tmp_path / "axiom-v2.db")
    metadata.create_all(engine)
    project = ProjectService(engine).create(CreateProject(title="Demo"))
    task = TaskService(engine).create(
        CreateTask(project_id=project.id, title="Task", origin="example.com", goal="finish")
    )
    app = create_v2_app(
        engine=engine,
        data_dir=tmp_path / "data",
        service_token="service-token",
    )
    headers = {"Authorization": "Bearer service-token"}

    with TestClient(app) as client:
        built = client.post(
            f"/api/v1/internal/tasks/{task.id}/context",
            headers=headers,
            json={
                "attempt_id": "bootstrap-test",
                "phase": "bootstrap",
                "intent_id": "i001",
            },
        )
        assert built.status_code == 200
        phase_context = json.loads(Path(built.json()["resources"]["phase_context"]).read_text())
        assert phase_context["execution_packet"]["intent"]["id"] == "i001"
        assert phase_context["task"]["scope"]["targets"] == ["example.com"]

        expanded = client.post(
            f"/api/v1/internal/tasks/{task.id}/context/expand",
            headers=headers,
            json={"facts": ["origin"], "intents": ["i001"]},
        )
        assert expanded.status_code == 200
        assert expanded.json()["facts"][0]["description"] == "example.com"
        assert expanded.json()["intents"][0]["description"] == "bootstrap"

        related = client.get(
            f"/api/v1/internal/tasks/{task.id}/context/intents/i001/related",
            headers=headers,
        )
        assert related.status_code == 200
        assert related.json()["facts"][0]["id"] == "origin"


def test_reason_phase_context_contains_only_delta_after_previous_commit(tmp_path) -> None:
    engine = create_sqlite_engine(tmp_path / "axiom-v2.db")
    metadata.create_all(engine)
    project = ProjectService(engine).create(CreateProject(title="Demo"))
    task = TaskService(engine).create(
        CreateTask(project_id=project.id, title="Task", origin="example.com", goal="finish")
    )
    with engine.begin() as conn:
        conn.execute(
            facts.insert(),
            [
                {
                    "id": "f001",
                    "task_id": task.id,
                    "description": "old fact",
                    "created_at": "2026-07-20T00:00:01Z",
                },
                {
                    "id": "f002",
                    "task_id": task.id,
                    "description": "new fact",
                    "created_at": "2026-07-20T00:00:03Z",
                },
            ],
        )
        conn.execute(
            reason_workspaces.insert().values(
                id="rw_001",
                task_id=task.id,
                status="committed",
                context_revision=1,
                draft_json="{}",
                worker="reasoner",
                failure_reason="",
                created_at="2026-07-20T00:00:01Z",
                updated_at="2026-07-20T00:00:02Z",
                committed_at="2026-07-20T00:00:02Z",
            )
        )

    snapshot = ContextBuilder(engine, data_dir=tmp_path / "data").build(
        task.id,
        attempt_id="reason-delta",
        phase="reason",
    )
    phase_context = json.loads(Path(snapshot.resources["phase_context"]).read_text())
    delta = phase_context["recent_delta"]
    assert delta["since"] == "2026-07-20T00:00:02Z"
    assert [item["id"] for item in delta["facts"]] == ["f002"]
    assert delta["new_fact_ids"] == ["f002"]
    assert delta["completed_intent_ids"] == []
    assert delta["failed_intent_ids"] == []
