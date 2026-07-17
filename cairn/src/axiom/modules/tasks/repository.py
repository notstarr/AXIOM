from __future__ import annotations

import json

from sqlalchemy import Connection, select

from axiom.infrastructure.database.metadata import (
    facts,
    id_counters,
    intent_sources,
    intents,
    task_hints,
    tasks,
)
from axiom.modules.common.ids import allocate_readable_id
from axiom.modules.tasks.schemas import CreateTask, Origin, Task, TaskScope


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
            "scope_enabled": True,
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
            created_at=now,
            updated_at=now,
            completed_at=None,
        )

    def get(self, conn: Connection, task_id: str) -> Task | None:
        row = conn.execute(select(tasks).where(tasks.c.id == task_id)).mappings().one_or_none()
        return self._to_model(row) if row is not None else None

    def list(self, conn: Connection) -> list[Task]:
        rows = conn.execute(select(tasks).order_by(tasks.c.created_at, tasks.c.id)).mappings()
        return [self._to_model(row) for row in rows]

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
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
        )
