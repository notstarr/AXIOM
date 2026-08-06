from __future__ import annotations

import json

from sqlalchemy import Connection, select, update

from axiom.modules.graph.tables import facts, intent_sources, intents
from axiom.modules.reason.tables import reason_run_sources
from axiom.modules.tasks.schemas import TaskScope
from axiom.modules.tasks.tables import task_hints, tasks


class ExecutionRepository:
    def input_row(self, conn: Connection, task_id: str, intent_id: str):
        return conn.execute(
            select(
                tasks.c.status.label("task_status"),
                tasks.c.project_id,
                tasks.c.guard_enabled,
                tasks.c.origin_input,
                tasks.c.goal,
                tasks.c.scope_json,
                tasks.c.worker_pool_json,
                intents.c.id.label("intent_id"),
                intents.c.todo_id,
                intents.c.description.label("intent_description"),
                intents.c.targets_json,
                intents.c.status.label("intent_status"),
                intents.c.worker,
                intents.c.lease_id,
                intents.c.claimed_at,
                intents.c.to_fact_id,
                intents.c.kind,
                intents.c.execute_timeout_seconds,
                intents.c.conclude_timeout_seconds,
                intents.c.retry_count,
                intents.c.infrastructure_retry_count,
            )
            .join(intents, intents.c.task_id == tasks.c.id)
            .where(tasks.c.id == task_id, intents.c.id == intent_id)
        ).mappings().one_or_none()

    def hints(self, conn: Connection, task_id: str) -> list[str]:
        return list(
            conn.execute(
                select(task_hints.c.content)
                .where(task_hints.c.task_id == task_id, task_hints.c.archived.is_(False))
                .order_by(task_hints.c.created_at, task_hints.c.id)
            ).scalars()
        )

    def source_facts(self, conn: Connection, task_id: str, intent_id: str) -> list[str]:
        return list(
            conn.execute(
                select(intent_sources.c.fact_id)
                .where(
                    intent_sources.c.task_id == task_id,
                    intent_sources.c.intent_id == intent_id,
                )
                .order_by(intent_sources.c.fact_id)
            ).scalars()
        )

    @staticmethod
    def transition_to_running(
        conn: Connection,
        *,
        task_id: str,
        intent_id: str,
        lease_id: str,
        now: str,
    ) -> bool:
        result = conn.execute(
            update(intents)
            .where(
                intents.c.task_id == task_id,
                intents.c.id == intent_id,
                intents.c.status == "claimed",
                intents.c.lease_id == lease_id,
            )
            .values(
                status="running",
                last_heartbeat_at=now,
                activity_kind="running",
                activity_summary="正在执行 Intent 并收集证据",
                activity_tool="",
                activity_started_at=now,
                activity_updated_at=now,
            )
        )
        return bool(result.rowcount)

    @staticmethod
    def heartbeat(
        conn: Connection,
        *,
        task_id: str,
        intent_id: str,
        lease_id: str,
        now: str,
    ) -> bool:
        result = conn.execute(
            update(intents)
            .where(
                intents.c.task_id == task_id,
                intents.c.id == intent_id,
                intents.c.lease_id == lease_id,
                intents.c.status.in_(["claimed", "running", "concluding"]),
            )
            .values(last_heartbeat_at=now, activity_updated_at=now)
        )
        return bool(result.rowcount)

    @staticmethod
    def update_activity(
        conn: Connection,
        *,
        task_id: str,
        intent_id: str,
        lease_id: str,
        activity_kind: str,
        activity_summary: str,
        activity_tool: str,
        now: str,
    ) -> bool:
        result = conn.execute(
            update(intents)
            .where(
                intents.c.task_id == task_id,
                intents.c.id == intent_id,
                intents.c.lease_id == lease_id,
                intents.c.status.in_(["claimed", "running", "concluding"]),
            )
            .values(
                activity_kind=activity_kind,
                activity_summary=activity_summary,
                activity_tool=activity_tool,
                activity_updated_at=now,
            )
        )
        return bool(result.rowcount)

    @staticmethod
    def transition_to_concluding(
        conn: Connection,
        *,
        task_id: str,
        intent_id: str,
        lease_id: str,
        now: str,
    ) -> bool:
        result = conn.execute(
            update(intents)
            .where(
                intents.c.task_id == task_id,
                intents.c.id == intent_id,
                intents.c.status == "running",
                intents.c.lease_id == lease_id,
            )
            .values(
                status="concluding",
                last_heartbeat_at=now,
                activity_kind="concluding",
                activity_summary="正在整理阶段产物并完成收口校验",
                activity_tool="",
                activity_updated_at=now,
            )
        )
        return bool(result.rowcount)

    @staticmethod
    def release(
        conn: Connection,
        *,
        task_id: str,
        intent_id: str,
        lease_id: str,
        failure_kind: str,
        failure_class: str,
        next_retry_at: str | None,
        terminal: bool,
        now: str,
    ) -> bool:
        result = conn.execute(
            update(intents)
            .where(
                intents.c.task_id == task_id,
                intents.c.id == intent_id,
                intents.c.lease_id == lease_id,
                intents.c.status.in_(["claimed", "running", "concluding"]),
            )
            .values(
                status="failed" if terminal else "pending",
                worker=None,
                lease_id=None,
                claimed_at=None,
                last_heartbeat_at=None,
                next_retry_at=None if terminal else next_retry_at,
                last_failure_kind=failure_kind,
                failure_class=failure_class,
                activity_kind="failed" if terminal else "retry_wait",
                activity_summary=(
                    f"执行失败：{failure_kind}" if terminal else f"执行释放，等待重试：{failure_kind}"
                ),
                activity_tool="",
                activity_updated_at=now,
                retry_count=intents.c.retry_count + (1 if failure_class == "execution" else 0),
                infrastructure_retry_count=intents.c.infrastructure_retry_count
                + (1 if failure_class == "transient" else 0),
                concluded_at=now if terminal else None,
            )
        )
        return bool(result.rowcount)

    @staticmethod
    def complete_intent(
        conn: Connection,
        *,
        task_id: str,
        intent_id: str,
        lease_id: str,
        fact_id: str,
        now: str,
    ) -> bool:
        result = conn.execute(
            update(intents)
            .where(
                intents.c.task_id == task_id,
                intents.c.id == intent_id,
                intents.c.lease_id == lease_id,
                intents.c.status.in_(["running", "concluding"]),
            )
            .values(
                status="completed",
                to_fact_id=fact_id,
                concluded_at=now,
                last_heartbeat_at=now,
                next_retry_at=None,
                last_failure_kind="",
                failure_class="",
                activity_kind="completed",
                activity_summary="Intent 已完成并写入 Fact",
                activity_tool="",
                activity_updated_at=now,
            )
        )
        return bool(result.rowcount)

    @staticmethod
    def complete_task_phase(
        conn: Connection,
        task_id: str,
        now: str,
        *,
        phase: str = "reason",
    ) -> None:
        conn.execute(
            update(tasks)
            .where(tasks.c.id == task_id, tasks.c.status == "running")
            .values(phase=phase, updated_at=now)
        )

    @staticmethod
    def reason_run_for_fact(conn: Connection, task_id: str, fact_id: str) -> str | None:
        return conn.execute(
            select(reason_run_sources.c.reason_run_id)
            .where(
                reason_run_sources.c.task_id == task_id,
                reason_run_sources.c.fact_id == fact_id,
            )
            .order_by(reason_run_sources.c.reason_run_id)
        ).scalar_one_or_none()

    @staticmethod
    def fact(conn: Connection, task_id: str, fact_id: str):
        return conn.execute(
            select(facts).where(facts.c.task_id == task_id, facts.c.id == fact_id)
        ).mappings().one_or_none()

    @staticmethod
    def parse_scope(row) -> dict[str, object]:
        scope = TaskScope.model_validate(json.loads(row["scope_json"] or "{}"))
        return scope.model_dump(mode="json") if scope.enabled else {}
