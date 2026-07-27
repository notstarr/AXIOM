from __future__ import annotations

import json

from sqlalchemy import Engine, func, select, update

from axiom.modules.graph.repository import GraphRepository
from axiom.modules.graph.tables import facts, intents
from axiom.modules.projects.repository import ProjectRepository
from axiom.modules.reason.repository import ReasonRepository
from axiom.modules.reason.tables import reason_runs
from axiom.modules.tasks.origin import parse_origin
from axiom.modules.tasks.repository import TaskRepository
from axiom.modules.tasks.schemas import (
    ActiveReasonRun,
    CreateTask,
    CreateTaskHint,
    Task,
    TaskHint,
    TaskScope,
    UpdateTask,
    UpdateTaskHint,
)
from axiom.modules.tasks.tables import task_hints
from axiom.modules.todos.tables import todos
from axiom.modules.vulnerabilities.tables import vulnerabilities
from axiom.shared.clock import utcnow
from axiom.shared.errors import ConflictError, NotFoundError


class TaskService:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.projects = ProjectRepository()
        self.tasks = TaskRepository()
        self.graph = GraphRepository()
        self.reason = ReasonRepository()

    def create(self, command: CreateTask) -> Task:
        origin = parse_origin(command.origin)
        scope = command.scope or TaskScope(enabled=True, targets=[origin.host])
        if scope.enabled and not scope.targets:
            scope = scope.model_copy(update={"targets": [origin.host]})
        now = utcnow()
        with self.engine.begin() as conn:
            project = self.projects.get(conn, command.project_id)
            if project is None:
                raise NotFoundError(f"Project not found: {command.project_id}")
            if project.status != "active":
                raise ConflictError("Task cannot be created in an archived Project")
            task = self.tasks.create(
                conn,
                project_id=command.project_id,
                title=command.title,
                origin=origin,
                goal=command.goal,
                scope=scope,
                worker_pool=command.worker_pool,
                guard_enabled=command.guard_enabled,
                external_report_enabled=command.external_report_enabled,
                hints=command.hints,
                now=now,
            )
            self.graph.seed_task(conn, task=task, now=now)
        return self._decorate(task)

    def get(self, task_id: str) -> Task:
        with self.engine.connect() as conn:
            task = self.tasks.get(conn, task_id)
        if task is None:
            raise NotFoundError(f"Task not found: {task_id}")
        return self._decorate(task)

    def update(self, task_id: str, command: UpdateTask) -> Task:
        with self.engine.begin() as conn:
            task = self.tasks.get(conn, task_id)
            if task is None:
                raise NotFoundError(f"Task not found: {task_id}")
            if task.legacy_readonly:
                raise ConflictError("Legacy Task is read-only")
            if command.scope is not None and command.scope.enabled and not command.scope.targets:
                command = command.model_copy(
                    update={"scope": command.scope.model_copy(update={"targets": [task.origin.host]})}
                )
            now = utcnow()
            updated = self.tasks.update(conn, task_id, command, now=now)
            if command.goal is not None:
                conn.execute(
                    update(facts)
                    .where(facts.c.task_id == task_id, facts.c.id == "goal")
                    .values(description=command.goal)
                )
            if command.worker_pool is not None:
                conn.execute(
                    update(intents)
                    .where(intents.c.task_id == task_id, intents.c.status == "pending")
                    .values(worker_pool_json=json.dumps(command.worker_pool, ensure_ascii=False))
                )
                conn.execute(
                    update(reason_runs)
                    .where(reason_runs.c.task_id == task_id, reason_runs.c.status == "pending")
                    .values(
                        worker_pool_json=json.dumps(command.worker_pool, ensure_ascii=False),
                        updated_at=now,
                    )
                )
        assert updated is not None
        return self._decorate(updated)

    def list(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        phase: str | None = None,
        search: str = "",
    ) -> list[Task]:
        with self.engine.connect() as conn:
            result = self.tasks.list(
                conn,
                project_id=project_id,
                status=status,
                phase=phase,
                search=search,
            )
        return [self._decorate(task) for task in result]

    def stop(self, task_id: str) -> Task:
        with self.engine.begin() as conn:
            task = self.tasks.get(conn, task_id)
            if task is None:
                raise NotFoundError(f"Task not found: {task_id}")
            if task.status != "running":
                raise ConflictError("Only a running Task can be stopped")
            now = utcnow()
            self.graph.cancel_active_intents(conn, task_id, now=now)
            self.reason.abandon_open(
                conn,
                task_id=task_id,
                failure_reason="task_stopped",
                now=now,
            )
            self.reason.cancel_open_for_task(conn, task_id, now=now)
            stopped = self.tasks.set_lifecycle(
                conn,
                task_id,
                status="stopped",
                phase="none",
                now=now,
            )
            assert stopped is not None
        return self._decorate(stopped)

    def resume(self, task_id: str) -> Task:
        with self.engine.begin() as conn:
            task = self.tasks.get(conn, task_id)
            if task is None:
                raise NotFoundError(f"Task not found: {task_id}")
            if task.status != "stopped":
                raise ConflictError("Only a stopped Task can be resumed")
            self.graph.restore_task_stopped_intents(conn, task_id)
            bootstrap = self.graph.get_intent(conn, task_id, "i001")
            phase = "waiting" if bootstrap and bootstrap.status == "completed" else "bootstrap"
            resumed = self.tasks.set_lifecycle(
                conn,
                task_id,
                status="running",
                phase=phase,
                now=utcnow(),
            )
            assert resumed is not None
        return self._decorate(resumed)

    def delete(self, task_id: str) -> None:
        with self.engine.begin() as conn:
            task = self.tasks.get(conn, task_id)
            if task is None:
                raise NotFoundError(f"Task not found: {task_id}")
            if task.status == "running":
                raise ConflictError("Stop a running Task before deleting it")
            self.tasks.delete(conn, task_id)

    def hints(self, task_id: str) -> list[TaskHint]:
        with self.engine.connect() as conn:
            if self.tasks.get(conn, task_id) is None:
                raise NotFoundError(f"Task not found: {task_id}")
            return self.tasks.hints(conn, task_id)

    def add_hint(self, task_id: str, command: CreateTaskHint) -> TaskHint:
        with self.engine.begin() as conn:
            task = self.tasks.get(conn, task_id)
            if task is None:
                raise NotFoundError(f"Task not found: {task_id}")
            if task.legacy_readonly:
                raise ConflictError("Legacy Task is read-only")
            return self.tasks.create_hint(conn, task_id=task_id, command=command, now=utcnow())

    def update_hint(self, task_id: str, hint_id: str, command: UpdateTaskHint) -> TaskHint:
        with self.engine.begin() as conn:
            task = self.tasks.get(conn, task_id)
            if task is None:
                raise NotFoundError(f"Task not found: {task_id}")
            if task.legacy_readonly:
                raise ConflictError("Legacy Task is read-only")
            updated = self.tasks.update_hint(
                conn,
                task_id=task_id,
                hint_id=hint_id,
                command=command,
                now=utcnow(),
            )
        if updated is None:
            raise NotFoundError(f"Hint not found: {hint_id}")
        return updated

    def _decorate(self, task: Task) -> Task:
        """Attach live counters and the current Reason activity to a Task."""
        with self.engine.connect() as conn:
            counts = {
                "fact_count": conn.execute(
                    select(func.count()).select_from(facts).where(
                        facts.c.task_id == task.id, facts.c.archived.is_(False)
                    )
                ).scalar_one(),
                "intent_count": conn.execute(
                    select(func.count()).select_from(intents).where(
                        intents.c.task_id == task.id, intents.c.archived.is_(False)
                    )
                ).scalar_one(),
                "working_intent_count": conn.execute(
                    select(func.count()).select_from(intents).where(
                        intents.c.task_id == task.id,
                        intents.c.archived.is_(False),
                        intents.c.status.in_(["claimed", "running", "concluding"]),
                        intents.c.kind == "explore",
                    )
                ).scalar_one(),
                "hint_count": conn.execute(
                    select(func.count()).select_from(task_hints).where(
                        task_hints.c.task_id == task.id, task_hints.c.archived.is_(False)
                    )
                ).scalar_one(),
                "todo_count": conn.execute(
                    select(func.count()).select_from(todos).where(
                        todos.c.task_id == task.id, todos.c.archived.is_(False)
                    )
                ).scalar_one(),
                "vulnerability_count": conn.execute(
                    select(func.count()).select_from(vulnerabilities).where(
                        vulnerabilities.c.task_id == task.id,
                        vulnerabilities.c.archived.is_(False),
                    )
                ).scalar_one(),
            }
            reason = conn.execute(
                select(reason_runs)
                .where(
                    reason_runs.c.task_id == task.id,
                    reason_runs.c.status.in_(["pending", "claimed", "running", "concluding"]),
                )
                .order_by(reason_runs.c.created_at.desc(), reason_runs.c.id.desc())
                .limit(1)
            ).mappings().one_or_none()
        reason_present = reason is not None
        return task.model_copy(
            update={
                **counts,
                "reason_worker": reason["worker"] if reason_present else None,
                "reason_heartbeat_at": reason["last_heartbeat_at"] if reason_present else None,
                "reason_activity_kind": reason["activity_kind"] if reason_present else "",
                "reason_activity_summary": (
                    reason["activity_summary"]
                    or "正在分析任务上下文并规划下一批 Intent"
                ) if reason_present else "",
                "reason_activity_tool": reason["activity_tool"] if reason_present else "",
                "reason_activity_started_at": (
                    (reason["activity_started_at"] or reason["claimed_at"])
                    if reason_present else None
                ),
                "reason_activity_updated_at": (
                    (reason["activity_updated_at"] or reason["last_heartbeat_at"])
                    if reason_present else None
                ),
                "active_reason_run": (
                    ActiveReasonRun(
                        id=reason["id"],
                        status=reason["status"],
                        trigger=reason["trigger"],
                        trigger_payload=json.loads(reason["trigger_payload_json"] or "{}"),
                        worker=reason["worker"],
                        lease_id=reason["lease_id"],
                        settings_version=reason["settings_version"],
                        execute_timeout_seconds=reason["execute_timeout_seconds"],
                        conclude_timeout_seconds=reason["conclude_timeout_seconds"],
                        last_heartbeat_at=reason["last_heartbeat_at"],
                        activity_kind=reason["activity_kind"] or "",
                        activity_summary=reason["activity_summary"] or "",
                        activity_tool=reason["activity_tool"] or "",
                        activity_started_at=reason["activity_started_at"],
                        activity_updated_at=reason["activity_updated_at"],
                        created_at=reason["created_at"],
                    )
                    if reason_present
                    else None
                ),
            }
        )
