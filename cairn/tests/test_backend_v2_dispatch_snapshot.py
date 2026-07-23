from __future__ import annotations

import json

from sqlalchemy import update

from axiom.dispatcher.v2.snapshot import DispatcherSnapshotService
from axiom.dispatcher.v2.worker_allocator import WorkerAllocator
from axiom.infrastructure.database.engine import create_sqlite_engine
from axiom.infrastructure.database.metadata import (
    context_revisions,
    intents,
    metadata,
    reason_workspaces,
    tasks,
)
from axiom.modules.graph.service import IntentService
from axiom.modules.projects.schemas import CreateProject
from axiom.modules.projects.service import ProjectService
from axiom.modules.tasks.schemas import CreateTask
from axiom.modules.tasks.service import TaskService
from axiom.modules.workers.schemas import UpsertWorker
from axiom.modules.workers.service import WorkerService


def make_domain(tmp_path):
    engine = create_sqlite_engine(tmp_path / "axiom-v2.db")
    metadata.create_all(engine)
    project = ProjectService(engine).create(CreateProject(title="Demo"))
    task = TaskService(engine).create(
        CreateTask(project_id=project.id, title="Task", origin="example.com", goal="finish")
    )
    workers = WorkerService(engine)
    workers.upsert(
        UpsertWorker(
            name="worker-a",
            capabilities=["bootstrap", "reason", "explore"],
            max_running=2,
            priority=0,
            config={"model": "test-model"},
        )
    )
    return engine, task, workers


def test_dispatch_snapshot_derives_running_counts_from_durable_leases(tmp_path) -> None:
    engine, task, workers = make_domain(tmp_path)
    snapshot = DispatcherSnapshotService(engine).build()
    assert snapshot.tasks[0].id == task.id
    assert snapshot.tasks[0].phase == "bootstrap"
    assert snapshot.workers[0].running == 0

    IntentService(engine).claim(task.id, "i001", worker="worker-a")
    restarted_service = DispatcherSnapshotService(engine)
    restarted_snapshot = restarted_service.build()
    assert restarted_snapshot.workers[0].running == 1

    workers.set_cooldown("worker-a", failure_kind="hard_timeout", cooldown_until="2099-01-01T00:00:00Z")
    persisted = DispatcherSnapshotService(engine).build().workers[0]
    assert persisted.cooldown_until == "2099-01-01T00:00:00Z"
    assert WorkerAllocator().choose([persisted], capability="bootstrap", task_pool=[]) is None


def test_dispatch_snapshot_marks_reason_due_for_terminal_intent_after_last_reason_context(
    tmp_path,
) -> None:
    engine, task, _workers = make_domain(tmp_path)
    with engine.begin() as conn:
        conn.execute(
            context_revisions.insert().values(
                task_id=task.id,
                revision=1,
                graph_path="/tmp/graph.yaml",
                todo_path="/tmp/todos.json",
                artifact_index_path="/tmp/artifacts.json",
                hints_path="/tmp/hints.md",
                knowledge_path="/tmp/knowledge.json",
                hash_manifest_json="{}",
                created_at="2026-07-22T00:00:00Z",
            )
        )
        conn.execute(
            reason_workspaces.insert().values(
                id="rw_000001",
                task_id=task.id,
                status="committed",
                context_revision=1,
                draft_json=json.dumps({}),
                worker="reasoner",
                failure_reason="",
                created_at="2026-07-22T00:00:01Z",
                updated_at="2026-07-22T00:00:10Z",
                committed_at="2026-07-22T00:00:10Z",
            )
        )
        conn.execute(
            intents.insert().values(
                id="i002",
                task_id=task.id,
                description="finished while Reason was running",
                status="completed",
                creator="reasoner",
                worker_pool_json="[]",
                targets_json='["example.com"]',
                retry_count=0,
                last_failure_kind="",
                created_at="2026-07-22T00:00:02Z",
                concluded_at="2026-07-22T00:00:05Z",
                archived=False,
                activity_kind="finished",
                activity_summary="done",
                activity_tool="",
            )
        )
        conn.execute(
            update(tasks).where(tasks.c.id == task.id).values(phase="explore")
        )

    snapshot = DispatcherSnapshotService(engine).build().tasks[0]
    assert snapshot.phase == "explore"
    assert snapshot.reason_due is True
    assert snapshot.terminal_delta_count == 1
    assert snapshot.oldest_terminal_delta_at == "2026-07-22T00:00:05Z"
    assert snapshot.last_reason_committed_at == "2026-07-22T00:00:10Z"
    assert snapshot.open_explore_intent_count == 0


def test_dispatch_snapshot_counts_open_intents_and_retry_exhaustion_delta(tmp_path) -> None:
    engine, task, _workers = make_domain(tmp_path)
    with engine.begin() as conn:
        conn.execute(
            context_revisions.insert().values(
                task_id=task.id,
                revision=1,
                graph_path="/tmp/graph.yaml",
                todo_path="/tmp/todos.json",
                artifact_index_path="/tmp/artifacts.json",
                hints_path="/tmp/hints.md",
                knowledge_path="/tmp/knowledge.json",
                hash_manifest_json="{}",
                created_at="2026-07-22T00:00:00Z",
            )
        )
        conn.execute(
            reason_workspaces.insert().values(
                id="rw_000001",
                task_id=task.id,
                status="committed",
                context_revision=1,
                draft_json=json.dumps({}),
                worker="reasoner",
                failure_reason="",
                created_at="2026-07-22T00:00:01Z",
                updated_at="2026-07-22T00:00:02Z",
                committed_at="2026-07-22T00:00:02Z",
            )
        )
        conn.execute(
            intents.insert(),
            [
                {
                    "id": "i002",
                    "task_id": task.id,
                    "description": "still running",
                    "status": "running",
                    "creator": "reasoner",
                    "worker": "worker-a",
                    "worker_pool_json": "[]",
                    "targets_json": '["example.com"]',
                    "retry_count": 0,
                    "last_failure_kind": "",
                    "failure_class": "",
                    "created_at": "2026-07-22T00:00:03Z",
                    "concluded_at": None,
                    "archived": False,
                    "activity_kind": "testing",
                    "activity_summary": "running",
                    "activity_tool": "",
                },
                {
                    "id": "i003",
                    "task_id": task.id,
                    "description": "retry exhausted",
                    "status": "failed",
                    "creator": "reasoner",
                    "worker": None,
                    "worker_pool_json": "[]",
                    "targets_json": '["example.com"]',
                    "retry_count": 4,
                    "last_failure_kind": "retry_exhausted",
                    "failure_class": "planning",
                    "created_at": "2026-07-22T00:00:03Z",
                    "concluded_at": "2026-07-22T00:00:04Z",
                    "archived": False,
                    "activity_kind": "failed",
                    "activity_summary": "replan",
                    "activity_tool": "",
                },
            ],
        )
        conn.execute(update(tasks).where(tasks.c.id == task.id).values(phase="explore"))

    snapshot = DispatcherSnapshotService(engine).build().tasks[0]
    assert snapshot.open_explore_intent_count == 1
    assert snapshot.terminal_delta_count == 1
    assert snapshot.needs_replan_due is True
