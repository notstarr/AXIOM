from __future__ import annotations

import json

from sqlalchemy import Connection, select, update

from axiom.modules.graph.schemas import Fact, Intent, TaskGraph
from axiom.modules.graph.tables import facts, intent_sources, intents
from axiom.modules.goals.tables import goal_evaluations
from axiom.modules.tasks.schemas import Task
from axiom.shared.ids import initialize_counter


class GraphRepository:
    def seed_task(self, conn: Connection, *, task: Task, now: str) -> None:
        initialize_counter(conn, scope=task.id, kind="fact", next_value=1)
        initialize_counter(conn, scope=task.id, kind="intent", next_value=2)
        conn.execute(
            facts.insert(),
            [
                {
                    "id": "origin",
                    "task_id": task.id,
                    "description": task.origin.original_input,
                    "created_at": now,
                    "archived": False,
                },
                {
                    "id": "goal",
                    "task_id": task.id,
                    "description": task.goal,
                    "created_at": now,
                    "archived": False,
                },
            ],
        )
        conn.execute(
            intents.insert().values(
                id="i001",
                task_id=task.id,
                todo_id=None,
                kind="bootstrap",
                description="bootstrap",
                status="pending",
                creator="dispatcher.bootstrap",
                worker=None,
                worker_pool_json=json.dumps(task.worker_pool, ensure_ascii=False),
                targets_json=json.dumps([task.origin.host], ensure_ascii=False),
                retry_count=0,
                infrastructure_retry_count=0,
                last_failure_kind="",
                failure_class="",
                created_at=now,
                archived=False,
            )
        )
        conn.execute(
            intent_sources.insert().values(
                task_id=task.id,
                intent_id="i001",
                fact_id="origin",
            )
        )

    def get_intent(self, conn: Connection, task_id: str, intent_id: str) -> Intent | None:
        row = conn.execute(
            select(intents).where(intents.c.task_id == task_id, intents.c.id == intent_id)
        ).mappings().one_or_none()
        return self._intent(conn, row) if row is not None else None

    def get_fact(self, conn: Connection, task_id: str, fact_id: str) -> Fact | None:
        row = conn.execute(
            select(facts).where(facts.c.task_id == task_id, facts.c.id == fact_id)
        ).mappings().one_or_none()
        return Fact.model_validate(row) if row is not None else None

    def update_fact(
        self,
        conn: Connection,
        task_id: str,
        fact_id: str,
        values: dict,
    ) -> Fact | None:
        if values:
            result = conn.execute(
                update(facts)
                .where(facts.c.task_id == task_id, facts.c.id == fact_id)
                .values(**values)
            )
            if not result.rowcount:
                return None
        return self.get_fact(conn, task_id, fact_id)

    def update_intent(
        self,
        conn: Connection,
        task_id: str,
        intent_id: str,
        values: dict,
    ) -> Intent | None:
        if values:
            result = conn.execute(
                update(intents)
                .where(intents.c.task_id == task_id, intents.c.id == intent_id)
                .values(**values)
            )
            if not result.rowcount:
                return None
        return self.get_intent(conn, task_id, intent_id)

    @staticmethod
    def fact_has_visible_intents(conn: Connection, task_id: str, fact_id: str) -> bool:
        return (
            conn.execute(
                select(intent_sources.c.intent_id)
                .join(
                    intents,
                    (intents.c.task_id == intent_sources.c.task_id)
                    & (intents.c.id == intent_sources.c.intent_id),
                )
                .where(
                    intent_sources.c.task_id == task_id,
                    intent_sources.c.fact_id == fact_id,
                    intents.c.archived.is_(False),
                )
                .limit(1)
            ).scalar_one_or_none()
            is not None
        )

    def graph(self, conn: Connection, task_id: str) -> TaskGraph:
        fact_rows = conn.execute(
            select(facts)
            .where(facts.c.task_id == task_id, facts.c.archived.is_(False))
            .order_by(facts.c.created_at, facts.c.id)
        ).mappings()
        intent_rows = conn.execute(
            select(intents)
            .where(intents.c.task_id == task_id, intents.c.archived.is_(False))
            .order_by(intents.c.created_at, intents.c.id)
        ).mappings()
        latest_goal_refs = conn.execute(
            select(goal_evaluations.c.evidence_refs_json)
            .where(
                goal_evaluations.c.task_id == task_id,
                goal_evaluations.c.achieved.is_(True),
                goal_evaluations.c.reason_run_id.is_not(None),
            )
            .order_by(goal_evaluations.c.created_at.desc(), goal_evaluations.c.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        goal_from_fact_ids = []
        if latest_goal_refs:
            goal_from_fact_ids = list(
                dict.fromkeys(
                    item_id
                    for ref in json.loads(latest_goal_refs)
                    for kind, separator, item_id in [ref.partition(":")]
                    if separator and kind == "fact" and item_id not in {"origin", "goal"}
                )
            )
        return TaskGraph(
            facts=[Fact.model_validate(row) for row in fact_rows],
            intents=[self._intent(conn, row) for row in intent_rows],
            goal_from_fact_ids=goal_from_fact_ids,
        )

    @staticmethod
    def _intent(conn: Connection, row) -> Intent:
        sources = list(
            conn.execute(
                select(intent_sources.c.fact_id)
                .where(
                    intent_sources.c.task_id == row["task_id"],
                    intent_sources.c.intent_id == row["id"],
                )
                .order_by(intent_sources.c.fact_id)
            ).scalars()
        )
        return Intent(
            id=row["id"],
            task_id=row["task_id"],
            todo_id=row["todo_id"],
            kind=row["kind"],
            from_facts=sources,
            description=row["description"],
            status=row["status"],
            creator=row["creator"],
            worker=row["worker"],
            worker_pool=json.loads(row["worker_pool_json"] or "[]"),
            targets=json.loads(row["targets_json"] or "[]"),
            execute_timeout_seconds=row["execute_timeout_seconds"],
            conclude_timeout_seconds=row["conclude_timeout_seconds"],
            lease_id=row["lease_id"],
            claimed_at=row["claimed_at"],
            last_heartbeat_at=row["last_heartbeat_at"],
            retry_count=row["retry_count"],
            infrastructure_retry_count=row["infrastructure_retry_count"],
            last_failure_kind=row["last_failure_kind"],
            failure_class=row["failure_class"],
            activity_kind=row["activity_kind"],
            activity_summary=row["activity_summary"],
            activity_tool=row["activity_tool"],
            activity_started_at=row["activity_started_at"],
            activity_updated_at=row["activity_updated_at"],
            next_retry_at=row["next_retry_at"],
            to_fact_id=row["to_fact_id"],
            created_at=row["created_at"],
            concluded_at=row["concluded_at"],
            archived=bool(row["archived"]),
        )

    @staticmethod
    def cancel_active_intents(
        conn: Connection,
        task_id: str,
        *,
        now: str,
        failure_kind: str = "task_stopped",
    ) -> int:
        result = conn.execute(
            update(intents)
            .where(
                intents.c.task_id == task_id,
                intents.c.status.in_(["claimed", "running", "concluding"]),
            )
            .values(
                status="cancelled",
                worker=None,
                lease_id=None,
                claimed_at=None,
                last_heartbeat_at=None,
                next_retry_at=None,
                concluded_at=now,
                last_failure_kind=failure_kind,
                failure_class="",
                activity_kind="cancelled",
                activity_summary="任务已暂停，当前执行已取消",
                activity_tool="",
                activity_updated_at=now,
            )
        )
        return int(result.rowcount or 0)

    @staticmethod
    def restore_task_stopped_intents(conn: Connection, task_id: str) -> int:
        result = conn.execute(
            update(intents)
            .where(
                intents.c.task_id == task_id,
                intents.c.status == "cancelled",
                intents.c.last_failure_kind == "task_stopped",
            )
            .values(
                status="pending",
                worker=None,
                lease_id=None,
                claimed_at=None,
                last_heartbeat_at=None,
                next_retry_at=None,
                concluded_at=None,
                last_failure_kind="",
                failure_class="",
                activity_kind="pending",
                activity_summary="任务已恢复，等待空闲 Worker 领取",
                activity_tool="",
                activity_started_at=None,
                activity_updated_at=None,
            )
        )
        return int(result.rowcount or 0)
