from __future__ import annotations

import json

from sqlalchemy import Connection, delete, func, or_, select, update

from axiom.infrastructure.database.metadata import (
    facts,
    id_counters,
    intent_sources,
    intents,
    task_hints,
    tasks,
    todos,
    vulnerabilities,
)
from axiom.modules.common.ids import allocate_readable_id
from axiom.modules.tasks.schemas import CreateTask, Origin, Task, TaskScope, UpdateTask


class TaskRepository:
    def create(
        self,
        conn: Connection,
        command: CreateTask,
        *,
        origin: Origin,
        now: str,
    ) -> Task:
        task_id = allocate_readable_id(
            conn,
            scope="global",
            kind="task",
            prefix="t_",
        )
        scope = command.scope or TaskScope(enabled=True, targets=[origin.host])
        if scope.enabled and not scope.targets:
            scope = scope.model_copy(update={"targets": [origin.host]})
        task_values = {
            "id": task_id,
            "project_id": command.project_id,
            "title": command.title,
            "origin_host": origin.host,
            "origin_scheme": origin.scheme,
            "origin_port": origin.port,
            "origin_input": origin.original_input,
            "goal": command.goal,
            "status": "running",
            "phase": "bootstrap",
            "bootstrap_enabled": True,
            "guard_enabled": command.guard_enabled,
            "external_report_enabled": command.external_report_enabled,
            "scope_enabled": scope.enabled,
            "scope_json": scope.model_dump_json(),
            "worker_pool_json": json.dumps(command.worker_pool, ensure_ascii=False),
            "legacy_readonly": False,
            "created_at": now,
            "updated_at": now,
        }
        conn.execute(tasks.insert().values(**task_values))
        conn.execute(
            id_counters.insert().values(scope=task_id, kind="intent", next_value=2)
        )
        conn.execute(
            id_counters.insert().values(scope=task_id, kind="fact", next_value=1)
        )
        conn.execute(
            id_counters.insert().values(
                scope=task_id,
                kind="hint",
                next_value=len(command.hints) + 1,
            )
        )
        conn.execute(
            facts.insert(),
            [
                {
                    "id": "origin",
                    "task_id": task_id,
                    "description": origin.original_input,
                    "created_at": now,
                },
                {
                    "id": "goal",
                    "task_id": task_id,
                    "description": command.goal,
                    "created_at": now,
                },
            ],
        )
        conn.execute(
            intents.insert().values(
                id="i001",
                task_id=task_id,
                todo_id=None,
                description="bootstrap",
                status="pending",
                creator="dispatcher.bootstrap",
                worker_pool_json=json.dumps(command.worker_pool, ensure_ascii=False),
                targets_json=json.dumps([origin.host], ensure_ascii=False),
                created_at=now,
            )
        )
        conn.execute(
            intent_sources.insert().values(task_id=task_id, intent_id="i001", fact_id="origin")
        )
        for index, content in enumerate(command.hints, start=1):
            conn.execute(
                task_hints.insert().values(
                    id=f"h{index:03d}",
                    task_id=task_id,
                    content=content,
                    creator="human",
                    created_at=now,
                )
            )

        return Task(
            id=task_id,
            project_id=command.project_id,
            title=command.title,
            origin=origin,
            goal=command.goal,
            status="running",
            phase="bootstrap",
            scope=scope,
            guard_enabled=command.guard_enabled,
            external_report_enabled=command.external_report_enabled,
            worker_pool=command.worker_pool,
            legacy_readonly=False,
            reason_worker=None,
            reason_heartbeat_at=None,
            reason_activity_kind="",
            reason_activity_summary="",
            reason_activity_started_at=None,
            reason_activity_updated_at=None,
            created_at=now,
            updated_at=now,
            completed_at=None,
            fact_count=2,
            intent_count=1,
            working_intent_count=0,
            hint_count=len(command.hints),
            todo_count=0,
            vulnerability_count=0,
        )

    def get(self, conn: Connection, task_id: str) -> Task | None:
        row = conn.execute(
            self._summary_query().where(tasks.c.id == task_id)
        ).mappings().one_or_none()
        return self._to_model(row) if row is not None else None

    def list(
        self,
        conn: Connection,
        *,
        project_id: str | None = None,
        status: str | None = None,
        phase: str | None = None,
        search: str = "",
    ) -> list[Task]:
        query = self._summary_query()
        if project_id:
            query = query.where(tasks.c.project_id == project_id)
        if status:
            query = query.where(tasks.c.status == status)
        if phase:
            query = query.where(tasks.c.phase == phase)
        if search:
            pattern = f"%{search.strip()}%"
            query = query.where(
                or_(
                    tasks.c.id.ilike(pattern),
                    tasks.c.title.ilike(pattern),
                    tasks.c.origin_input.ilike(pattern),
                    tasks.c.origin_host.ilike(pattern),
                    tasks.c.goal.ilike(pattern),
                )
            )
        rows = conn.execute(query.order_by(tasks.c.created_at.desc(), tasks.c.id.desc())).mappings()
        return [self._to_model(row) for row in rows]

    def update(self, conn: Connection, task_id: str, command: UpdateTask, *, now: str) -> Task | None:
        values = command.model_dump(exclude_none=True)
        if "worker_pool" in values:
            values["worker_pool_json"] = json.dumps(values.pop("worker_pool"), ensure_ascii=False)
        if not values:
            return self.get(conn, task_id)
        values["updated_at"] = now
        result = conn.execute(update(tasks).where(tasks.c.id == task_id).values(**values))
        return self.get(conn, task_id) if result.rowcount else None

    def delete(self, conn: Connection, task_id: str) -> bool:
        return bool(conn.execute(delete(tasks).where(tasks.c.id == task_id)).rowcount)

    @staticmethod
    def _to_model(row) -> Task:
        return Task(
            id=row["id"],
            project_id=row["project_id"],
            title=row["title"],
            origin=Origin(
                host=row["origin_host"],
                scheme=row["origin_scheme"],
                port=row["origin_port"],
                original_input=row["origin_input"],
            ),
            goal=row["goal"],
            status=row["status"],
            phase=row["phase"],
            scope=TaskScope.model_validate_json(row["scope_json"]),
            guard_enabled=bool(row["guard_enabled"]),
            external_report_enabled=bool(row["external_report_enabled"]),
            worker_pool=json.loads(row["worker_pool_json"] or "[]"),
            legacy_readonly=bool(row["legacy_readonly"]),
            reason_worker=row["reason_worker"],
            reason_heartbeat_at=row["reason_heartbeat_at"],
            reason_activity_kind=row.get("reason_activity_kind") or "",
            reason_activity_summary=row.get("reason_activity_summary") or "",
            reason_activity_started_at=row.get("reason_activity_started_at"),
            reason_activity_updated_at=row.get("reason_activity_updated_at"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
            fact_count=int(row.get("fact_count") or 0),
            intent_count=int(row.get("intent_count") or 0),
            working_intent_count=int(row.get("working_intent_count") or 0),
            hint_count=int(row.get("hint_count") or 0),
            todo_count=int(row.get("todo_count") or 0),
            vulnerability_count=int(row.get("vulnerability_count") or 0),
        )

    @staticmethod
    def _summary_query():
        def count_where(table, *conditions):
            return (
                select(func.count())
                .select_from(table)
                .where(table.c.task_id == tasks.c.id, *conditions)
                .correlate(tasks)
                .scalar_subquery()
            )

        return select(
            tasks,
            count_where(facts, facts.c.archived.is_(False)).label("fact_count"),
            count_where(intents, intents.c.archived.is_(False)).label("intent_count"),
            count_where(
                intents,
                intents.c.archived.is_(False),
                intents.c.status.in_(("claimed", "running", "concluding")),
            ).label("working_intent_count"),
            count_where(task_hints, task_hints.c.archived.is_(False)).label("hint_count"),
            count_where(todos, todos.c.archived.is_(False)).label("todo_count"),
            count_where(vulnerabilities, vulnerabilities.c.archived.is_(False)).label(
                "vulnerability_count"
            ),
        )
