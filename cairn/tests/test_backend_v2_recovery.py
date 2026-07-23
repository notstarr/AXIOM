from __future__ import annotations

from sqlalchemy import update

from axiom.dispatcher.v2.recovery import LeaseRecoveryService
from axiom.infrastructure.database.engine import create_sqlite_engine
from axiom.infrastructure.database.metadata import intents, metadata, tasks
from axiom.modules.graph.service import IntentService
from axiom.modules.projects.schemas import CreateProject
from axiom.modules.projects.service import ProjectService
from axiom.modules.tasks.lifecycle import TaskLifecycleService
from axiom.modules.tasks.schemas import CreateTask
from axiom.modules.tasks.service import TaskService


def test_restart_recovery_releases_stale_intent_and_reason_leases(tmp_path) -> None:
    engine = create_sqlite_engine(tmp_path / "v2.db")
    metadata.create_all(engine)
    project = ProjectService(engine).create(CreateProject(title="Demo"))
    task = TaskService(engine).create(
        CreateTask(project_id=project.id, title="Task", origin="example.com", goal="finish")
    )
    IntentService(engine).claim(task.id, "i001", worker="worker-a")
    TaskLifecycleService(engine).claim_reason(task.id, worker="worker-b")
    with engine.begin() as conn:
        conn.execute(
            update(intents)
            .where(intents.c.task_id == task.id, intents.c.id == "i001")
            .values(last_heartbeat_at="2020-01-01T00:00:00Z")
        )
        conn.execute(
            update(tasks)
            .where(tasks.c.id == task.id)
            .values(reason_heartbeat_at="2020-01-01T00:00:00Z")
        )

    result = LeaseRecoveryService(engine).recover(timeout_seconds=60)
    assert result.intents_released == 1
    assert result.reasons_released == 1
    recovered_intent = IntentService(engine).get(task.id, "i001")
    assert recovered_intent.status == "pending"
    assert recovered_intent.retry_count == 0
    # Dispatcher restart recovery is ownership repair, not a provider
    # failure, so it must not consume the infrastructure retry budget.
    assert recovered_intent.infrastructure_retry_count == 0
    assert recovered_intent.failure_class == "transient"
    assert recovered_intent.last_failure_kind == "stale_lease_recovered"
    assert TaskService(engine).get(task.id).reason_worker is None
