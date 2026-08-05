from __future__ import annotations

import json

from sqlalchemy import Connection, delete, or_, select, update

from axiom.modules.tasks.schemas import (
    CreateTaskHint,
    Origin,
    Task,
    TaskHint,
    TaskScope,
    UpdateTask,
    UpdateTaskHint,
)
from axiom.modules.tasks.tables import task_hints, tasks
from axiom.shared.ids import allocate_id, initialize_counter


class TaskRepository:
    def create(
        self,
        conn: Connection,
        *,
        project_id: str,
        title: str,
        origin: Origin,
        goal: str,
        scope: TaskScope,
        worker_pool: list[str],
        guard_enabled: bool,
        external_report_enabled: bool,
        run_duration_seconds: int | None,
        run_started_at: str,
        run_deadline_at: str | None,
        hints: list[str],
        now: str,
    ) -> Task:
        task_id = allocate_id(
            conn,
            scope="global",
            kind="task",
            prefix="t_",
        )
        values = {
            "id": task_id,
            "project_id": project_id,
            "title": title,
            "origin_host": origin.host,
            "origin_scheme": origin.scheme,
            "origin_port": origin.port,
            "origin_input": origin.original_input,
            "goal": goal,
            "current_goal_cycle_id": None,
            "status": "running",
            "phase": "bootstrap",
            "scope_json": scope.model_dump_json(),
            "worker_pool_json": json.dumps(worker_pool, ensure_ascii=False),
            "guard_enabled": guard_enabled,
            "external_report_enabled": external_report_enabled,
            "legacy_readonly": False,
            "archived": False,
            "created_at": now,
            "updated_at": now,
            "completed_at": None,
            "run_duration_seconds": run_duration_seconds,
            "run_started_at": run_started_at,
            "run_deadline_at": run_deadline_at,
            "last_pause_reason": None,
        }
        conn.execute(tasks.insert().values(**values))
        initialize_counter(conn, scope=task_id, kind="hint")
        for content in hints:
            conn.execute(
                task_hints.insert().values(
                    id=allocate_id(
                        conn,
                        scope=task_id,
                        kind="hint",
                        prefix="h",
                    ),
                    task_id=task_id,
                    content=content,
                    creator="human",
                    created_at=now,
                    archived=False,
                )
            )
        return self._task(values)

    def get(self, conn: Connection, task_id: str) -> Task | None:
        row = conn.execute(select(tasks).where(tasks.c.id == task_id)).mappings().one_or_none()
        return self._task(row) if row is not None else None

    def list(
        self,
        conn: Connection,
        *,
        project_id: str | None = None,
        status: str | None = None,
        phase: str | None = None,
        search: str = "",
    ) -> list[Task]:
        statement = select(tasks)
        if project_id:
            statement = statement.where(tasks.c.project_id == project_id)
        if status:
            statement = statement.where(tasks.c.status == status)
        if phase:
            statement = statement.where(tasks.c.phase == phase)
        if search.strip():
            pattern = f"%{search.strip()}%"
            statement = statement.where(
                or_(
                    tasks.c.id.ilike(pattern),
                    tasks.c.title.ilike(pattern),
                    tasks.c.origin_input.ilike(pattern),
                    tasks.c.goal.ilike(pattern),
                )
            )
        rows = conn.execute(
            statement.order_by(tasks.c.created_at.desc(), tasks.c.id.desc())
        ).mappings()
        return [self._task(row) for row in rows]

    def set_lifecycle(
        self,
        conn: Connection,
        task_id: str,
        *,
        status: str,
        phase: str,
        now: str,
    ) -> Task | None:
        result = conn.execute(
            update(tasks)
            .where(tasks.c.id == task_id)
            .values(status=status, phase=phase, updated_at=now)
        )
        return self.get(conn, task_id) if result.rowcount else None

    def pause_running(
        self,
        conn: Connection,
        task_id: str,
        *,
        reason: str,
        now: str,
        only_if_due: bool = False,
    ) -> Task | None:
        conditions = [tasks.c.id == task_id, tasks.c.status == "running"]
        if only_if_due:
            conditions.extend(
                [
                    tasks.c.run_deadline_at.is_not(None),
                    tasks.c.run_deadline_at <= now,
                ]
            )
        result = conn.execute(
            update(tasks)
            .where(*conditions)
            .values(
                status="stopped",
                phase="none",
                last_pause_reason=reason,
                updated_at=now,
            )
        )
        return self.get(conn, task_id) if result.rowcount else None

    def resume_with_window(
        self,
        conn: Connection,
        task_id: str,
        *,
        phase: str,
        run_duration_seconds: int | None,
        run_started_at: str,
        run_deadline_at: str | None,
        now: str,
    ) -> Task | None:
        result = conn.execute(
            update(tasks)
            .where(tasks.c.id == task_id, tasks.c.status == "stopped")
            .values(
                status="running",
                phase=phase,
                run_duration_seconds=run_duration_seconds,
                run_started_at=run_started_at,
                run_deadline_at=run_deadline_at,
                last_pause_reason=None,
                updated_at=now,
            )
        )
        return self.get(conn, task_id) if result.rowcount else None

    @staticmethod
    def expired_running_ids(conn: Connection, *, now: str) -> list[str]:
        return list(
            conn.execute(
                select(tasks.c.id)
                .where(
                    tasks.c.status == "running",
                    tasks.c.run_deadline_at.is_not(None),
                    tasks.c.run_deadline_at <= now,
                )
                .order_by(tasks.c.run_deadline_at, tasks.c.id)
            ).scalars()
        )

    def update(self, conn: Connection, task_id: str, command: UpdateTask, *, now: str) -> Task | None:
        values = command.model_dump(exclude_none=True)
        if command.scope is not None:
            values.pop("scope", None)
            values["scope_json"] = command.scope.model_dump_json()
        if command.worker_pool is not None:
            values.pop("worker_pool", None)
            values["worker_pool_json"] = json.dumps(command.worker_pool, ensure_ascii=False)
        if values:
            values["updated_at"] = now
            result = conn.execute(update(tasks).where(tasks.c.id == task_id).values(**values))
            if not result.rowcount:
                return None
        return self.get(conn, task_id)

    def delete(self, conn: Connection, task_id: str) -> bool:
        return bool(conn.execute(delete(tasks).where(tasks.c.id == task_id)).rowcount)

    def hints(self, conn: Connection, task_id: str) -> list[TaskHint]:
        rows = conn.execute(
            select(task_hints)
            .where(task_hints.c.task_id == task_id, task_hints.c.archived.is_(False))
            .order_by(task_hints.c.created_at, task_hints.c.id)
        ).mappings()
        return [TaskHint.model_validate(row) for row in rows]

    def create_hint(
        self,
        conn: Connection,
        *,
        task_id: str,
        command: CreateTaskHint,
        now: str,
    ) -> TaskHint:
        values = {
            "id": allocate_id(conn, scope=task_id, kind="hint", prefix="h"),
            "task_id": task_id,
            "content": command.content,
            "creator": command.creator,
            "created_at": now,
            "archived": False,
        }
        conn.execute(task_hints.insert().values(**values))
        return TaskHint.model_validate(values)

    def update_hint(
        self,
        conn: Connection,
        *,
        task_id: str,
        hint_id: str,
        command: UpdateTaskHint,
        now: str,
    ) -> TaskHint | None:
        values = command.model_dump(exclude_none=True)
        if values:
            result = conn.execute(
                update(task_hints)
                .where(task_hints.c.task_id == task_id, task_hints.c.id == hint_id)
                .values(**values)
            )
            if not result.rowcount:
                return None
        row = conn.execute(
            select(task_hints).where(task_hints.c.task_id == task_id, task_hints.c.id == hint_id)
        ).mappings().one_or_none()
        return TaskHint.model_validate(row) if row is not None else None

    @staticmethod
    def _task(row) -> Task:
        return Task(
            id=row["id"],
            project_id=row["project_id"],
            title=row["title"],
            origin=Origin(
                host=row["origin_host"],
                scheme=row["origin_scheme"],
                port=int(row["origin_port"]) if row["origin_port"] is not None else None,
                original_input=row["origin_input"],
            ),
            goal=row["goal"],
            current_goal_cycle_id=row.get("current_goal_cycle_id"),
            status=row["status"],
            phase=row["phase"],
            scope=TaskScope.model_validate_json(row["scope_json"]),
            worker_pool=json.loads(row["worker_pool_json"] or "[]"),
            guard_enabled=bool(row["guard_enabled"]),
            external_report_enabled=bool(row["external_report_enabled"]),
            legacy_readonly=bool(row["legacy_readonly"]),
            archived=bool(row.get("archived", False)),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
            run_duration_seconds=row.get("run_duration_seconds"),
            run_started_at=row.get("run_started_at"),
            run_deadline_at=row.get("run_deadline_at"),
            last_pause_reason=row.get("last_pause_reason"),
        )
