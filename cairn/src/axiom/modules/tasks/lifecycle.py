from __future__ import annotations

from sqlalchemy import Engine, select, update

from axiom.infrastructure.database.metadata import intents, tasks
from axiom.modules.common.time import utcnow
from axiom.modules.tasks.repository import TaskRepository
from axiom.modules.tasks.schemas import Task


class TaskLifecycleService:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.repository = TaskRepository()

    def claim_reason(self, task_id: str, *, worker: str) -> Task:
        worker = self._worker(worker)
        now = utcnow()
        with self.engine.begin() as conn:
            task = self._writable_running(conn, task_id)
            if task.reason_worker and task.reason_worker != worker:
                raise ValueError(f"Task Reason is currently claimed by {task.reason_worker}")
            conn.execute(
                update(tasks)
                .where(tasks.c.id == task_id)
                .values(reason_worker=worker, reason_heartbeat_at=now, phase="reason", updated_at=now)
            )
            return self.repository.get(conn, task_id)  # type: ignore[return-value]

    def heartbeat_reason(self, task_id: str, *, worker: str) -> Task:
        worker = self._worker(worker)
        with self.engine.begin() as conn:
            task = self._writable_running(conn, task_id)
            self._reason_owner(task, worker)
            conn.execute(
                update(tasks)
                .where(tasks.c.id == task_id)
                .values(reason_heartbeat_at=utcnow())
            )
            return self.repository.get(conn, task_id)  # type: ignore[return-value]

    def release_reason(self, task_id: str, *, worker: str) -> Task:
        worker = self._worker(worker)
        with self.engine.begin() as conn:
            task = self.repository.get(conn, task_id)
            if task is None:
                raise ValueError(f"Task not found: {task_id}")
            self._reason_owner(task, worker)
            conn.execute(
                update(tasks)
                .where(tasks.c.id == task_id)
                .values(reason_worker=None, reason_heartbeat_at=None, updated_at=utcnow())
            )
            return self.repository.get(conn, task_id)  # type: ignore[return-value]

    def stop(self, task_id: str) -> Task:
        now = utcnow()
        with self.engine.begin() as conn:
            task = self.repository.get(conn, task_id)
            if task is None:
                raise ValueError(f"Task not found: {task_id}")
            if task.legacy_readonly:
                raise ValueError("Legacy task is read-only")
            if task.status == "completed":
                raise ValueError("Completed Task cannot be stopped")
            conn.execute(
                update(intents)
                .where(
                    intents.c.task_id == task_id,
                    intents.c.status.in_(["claimed", "running", "concluding"]),
                )
                .values(status="pending", worker=None, last_heartbeat_at=None)
            )
            conn.execute(
                update(tasks)
                .where(tasks.c.id == task_id)
                .values(
                    status="stopped",
                    phase="none",
                    reason_worker=None,
                    reason_heartbeat_at=None,
                    updated_at=now,
                )
            )
            return self.repository.get(conn, task_id)  # type: ignore[return-value]

    def resume(self, task_id: str) -> Task:
        now = utcnow()
        with self.engine.begin() as conn:
            task = self.repository.get(conn, task_id)
            if task is None:
                raise ValueError(f"Task not found: {task_id}")
            if task.legacy_readonly:
                raise ValueError("Legacy task is read-only")
            if task.status != "stopped":
                raise ValueError("Only stopped Tasks can resume")
            bootstrap_status = conn.execute(
                select(intents.c.status).where(intents.c.task_id == task_id, intents.c.id == "i001")
            ).scalar_one_or_none()
            phase = "bootstrap" if bootstrap_status != "completed" else "reason"
            conn.execute(
                update(tasks)
                .where(tasks.c.id == task_id)
                .values(status="running", phase=phase, updated_at=now)
            )
            return self.repository.get(conn, task_id)  # type: ignore[return-value]

    def complete(self, task_id: str, *, description: str) -> Task:
        if not description.strip():
            raise ValueError("completion description must not be empty")
        now = utcnow()
        with self.engine.begin() as conn:
            task = self._writable_running(conn, task_id)
            conn.execute(
                update(intents)
                .where(
                    intents.c.task_id == task_id,
                    intents.c.status.not_in(["completed", "cancelled"]),
                )
                .values(status="cancelled", worker=None, last_heartbeat_at=None)
            )
            conn.execute(
                update(tasks)
                .where(tasks.c.id == task_id)
                .values(
                    status="completed",
                    phase="none",
                    reason_worker=None,
                    reason_heartbeat_at=None,
                    completed_at=now,
                    updated_at=now,
                )
            )
            return self.repository.get(conn, task_id)  # type: ignore[return-value]

    def _writable_running(self, conn, task_id: str) -> Task:
        task = self.repository.get(conn, task_id)
        if task is None:
            raise ValueError(f"Task not found: {task_id}")
        if task.legacy_readonly:
            raise ValueError("Legacy task is read-only")
        if task.status != "running":
            raise ValueError("Task is not running")
        return task

    @staticmethod
    def _reason_owner(task: Task, worker: str) -> None:
        if not task.reason_worker:
            raise ValueError("Task Reason is not currently claimed")
        if task.reason_worker != worker:
            raise ValueError(f"Task Reason is currently claimed by {task.reason_worker}")

    @staticmethod
    def _worker(worker: str) -> str:
        value = worker.strip()
        if not value:
            raise ValueError("worker must not be empty")
        return value
