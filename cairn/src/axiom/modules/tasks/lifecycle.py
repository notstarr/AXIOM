from __future__ import annotations

from sqlalchemy import Engine, select, update

from axiom.infrastructure.database.metadata import intents, tasks
from axiom.modules.audit.schemas import RecordAuditEvent
from axiom.modules.audit.service import AuditService
from axiom.modules.common.time import utcnow
from axiom.modules.tasks.repository import TaskRepository
from axiom.modules.tasks.schemas import Task


class TaskLifecycleService:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.repository = TaskRepository()
        self.audit = AuditService(engine)

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
                .values(
                    reason_worker=worker,
                    reason_heartbeat_at=now,
                    reason_activity_kind="starting",
                    reason_activity_summary="正在读取任务上下文",
                    reason_activity_started_at=now,
                    reason_activity_updated_at=now,
                    phase="reason",
                    updated_at=now,
                )
            )
            self.audit.record_in(conn, RecordAuditEvent(project_id=task.project_id, task_id=task_id, worker=worker, event_type="reason.claimed", message=f"Reason claimed by {worker}"))
            return self.repository.get(conn, task_id)  # type: ignore[return-value]

    def report_reason_activity(
        self,
        task_id: str,
        *,
        worker: str,
        kind: str,
        summary: str,
    ) -> Task:
        worker = self._worker(worker)
        kind = kind.strip()
        summary = summary.strip()
        if not kind:
            raise ValueError("Reason activity kind must not be empty")
        if not summary:
            raise ValueError("Reason activity summary must not be empty")
        now = utcnow()
        with self.engine.begin() as conn:
            task = self._writable_running(conn, task_id)
            self._reason_owner(task, worker)
            conn.execute(
                update(tasks)
                .where(tasks.c.id == task_id)
                .values(
                    reason_activity_kind=kind,
                    reason_activity_summary=summary,
                    reason_activity_started_at=task.reason_activity_started_at or now,
                    reason_activity_updated_at=now,
                    reason_heartbeat_at=now,
                    updated_at=now,
                )
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
            self.audit.record_in(conn, RecordAuditEvent(project_id=task.project_id, task_id=task_id, worker=worker, event_type="reason.released", message=f"Reason released by {worker}"))
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
                .values(
                    status="pending",
                    worker=None,
                    last_heartbeat_at=None,
                    activity_kind="stopped",
                    activity_summary="任务已停止",
                    activity_tool="",
                    activity_updated_at=now,
                )
            )
            conn.execute(
                update(tasks)
                .where(tasks.c.id == task_id)
                .values(
                    status="stopped",
                    phase="none",
                    reason_worker=None,
                    reason_heartbeat_at=None,
                    reason_activity_kind="stopped",
                    reason_activity_summary="任务已停止",
                    reason_activity_updated_at=now,
                    updated_at=now,
                )
            )
            self.audit.record_in(conn, RecordAuditEvent(project_id=task.project_id, task_id=task_id, event_type="task.stopped", message="Task stopped"))
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
            if phase == "bootstrap":
                # A Bootstrap that exhausted retries is deliberately parked as
                # `needs_hint`. Explicitly stopping and resuming is a human
                # retry decision, so clear that latch; otherwise the Task says
                # "running / bootstrap" forever while the reconciler refuses
                # to dispatch any Worker.
                conn.execute(
                    update(intents)
                    .where(intents.c.task_id == task_id, intents.c.id == "i001")
                    .values(
                        status="pending",
                        worker=None,
                        last_heartbeat_at=None,
                        activity_kind="",
                        activity_summary="",
                        activity_tool="",
                        activity_started_at=None,
                        activity_updated_at=None,
                        retry_count=0,
                        infrastructure_retry_count=0,
                        next_retry_at=None,
                        last_failure_kind="",
                        failure_class="",
                    )
                )
            conn.execute(
                update(tasks)
                .where(tasks.c.id == task_id)
                .values(
                    status="running",
                    phase=phase,
                    reason_activity_kind="",
                    reason_activity_summary="",
                    reason_activity_started_at=None,
                    reason_activity_updated_at=None,
                    updated_at=now,
                )
            )
            self.audit.record_in(conn, RecordAuditEvent(project_id=task.project_id, task_id=task_id, event_type="task.resumed", message=f"Task resumed at {phase}"))
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
                .values(
                    status="cancelled",
                    worker=None,
                    last_heartbeat_at=None,
                    activity_kind="cancelled",
                    activity_summary="任务目标已完成，执行已取消",
                    activity_tool="",
                    activity_updated_at=now,
                )
            )
            conn.execute(
                update(tasks)
                .where(tasks.c.id == task_id)
                .values(
                    status="completed",
                    phase="none",
                    reason_worker=None,
                    reason_heartbeat_at=None,
                    reason_activity_kind="finished",
                    reason_activity_summary="任务目标已完成",
                    reason_activity_updated_at=now,
                    completed_at=now,
                    updated_at=now,
                )
            )
            self.audit.record_in(conn, RecordAuditEvent(project_id=task.project_id, task_id=task_id, event_type="task.completed", message="Task Goal achieved", payload={"description": description}))
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
