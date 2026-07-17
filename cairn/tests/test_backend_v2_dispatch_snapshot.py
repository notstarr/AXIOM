from __future__ import annotations

from axiom.dispatcher.v2.snapshot import DispatcherSnapshotService
from axiom.dispatcher.v2.worker_allocator import WorkerAllocator
from axiom.infrastructure.database.engine import create_sqlite_engine
from axiom.infrastructure.database.metadata import metadata
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
