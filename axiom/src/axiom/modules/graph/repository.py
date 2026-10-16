from __future__ import annotations

import json
from collections import defaultdict

from sqlalchemy import Connection, func, select, update

from axiom.modules.graph.schemas import Fact, GraphChildren, GraphCounts, Intent, TaskGraph
from axiom.modules.graph.tables import facts, intent_sources, intents
from axiom.modules.goals.tables import goal_evaluations, task_goal_cycles
from axiom.modules.tasks.schemas import Task
from axiom.modules.tasks.tables import tasks
from axiom.shared.ids import initialize_counter


def _json_object(value) -> dict:
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


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
                goal_cycle_id=task.current_goal_cycle_id,
                todo_id=None,
                kind="bootstrap",
                description="bootstrap",
                priority=50,
                status="pending",
                creator="dispatcher.bootstrap",
                worker=None,
                worker_pool_json=json.dumps(task.worker_pool, ensure_ascii=False),
                targets_json="[]",
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

    @staticmethod
    def from_row(row, *, sources: list[str]) -> Intent:
        """Build an Intent from an already-loaded row and source relation list."""

        return GraphRepository._intent(None, row, sources=sources)

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

    def graph(self, conn: Connection, task_id: str, scope: str = "full") -> TaskGraph:
        # scope=skeleton：只回 origin/goal 两个骨架节点（外加任务级 Goal 元信息）。
        # 其余节点由前端按需调 children() 展开，避免首屏传输整张图。
        skeleton_only = scope == "skeleton"
        task_row = conn.execute(
            select(
                tasks.c.current_goal_cycle_id,
                tasks.c.status,
            ).where(tasks.c.id == task_id)
        ).mappings().one_or_none()
        current_goal_cycle_id = task_row["current_goal_cycle_id"] if task_row else None
        task_status = task_row["status"] if task_row else "running"
        fact_query = select(facts).where(facts.c.task_id == task_id, facts.c.archived.is_(False))
        if skeleton_only:
            fact_query = fact_query.where(facts.c.id.in_(("origin", "goal")))
        fact_rows = conn.execute(
            fact_query.order_by(facts.c.created_at, facts.c.id)
        ).mappings().all()
        visible_fact_ids = {row["id"] for row in fact_rows}
        intent_rows = [] if skeleton_only else conn.execute(
            select(intents)
            .where(intents.c.task_id == task_id, intents.c.archived.is_(False))
            .order_by(intents.c.created_at, intents.c.id)
        ).mappings().all()
        source_rows = [] if skeleton_only else conn.execute(
            select(intent_sources.c.intent_id, intent_sources.c.fact_id)
            .join(
                facts,
                (facts.c.task_id == intent_sources.c.task_id)
                & (facts.c.id == intent_sources.c.fact_id),
            )
            .where(
                intent_sources.c.task_id == task_id,
                facts.c.archived.is_(False),
            )
            .order_by(intent_sources.c.intent_id, intent_sources.c.fact_id)
        ).mappings().all()
        sources_by_intent: dict[str, list[str]] = defaultdict(list)
        for source in source_rows:
            sources_by_intent[source["intent_id"]].append(source["fact_id"])
        latest_goal_evaluation = conn.execute(
            select(goal_evaluations)
            .where(
                goal_evaluations.c.task_id == task_id,
                goal_evaluations.c.goal_cycle_id == current_goal_cycle_id,
                goal_evaluations.c.achieved.is_(True),
            )
            .order_by(goal_evaluations.c.created_at.desc(), goal_evaluations.c.id.desc())
            .limit(1)
        ).mappings().one_or_none()
        goal_status = (
            "completed"
            if task_status == "completed"
            else "active"
        )
        history = []
        cycle_rows = conn.execute(
            select(task_goal_cycles)
            .where(
                task_goal_cycles.c.task_id == task_id,
                task_goal_cycles.c.id != current_goal_cycle_id,
                task_goal_cycles.c.status == "completed",
            )
            .order_by(task_goal_cycles.c.sequence)
        ).mappings().all()
        cycle_ids = [cycle["id"] for cycle in cycle_rows]
        latest_evaluation_by_cycle: dict[str, dict] = {}
        if cycle_ids:
            evaluation_rows = conn.execute(
                select(
                    goal_evaluations.c.id,
                    goal_evaluations.c.goal_cycle_id,
                    goal_evaluations.c.evidence_refs_json,
                    goal_evaluations.c.reason,
                    goal_evaluations.c.evaluator,
                    goal_evaluations.c.reason_run_id,
                    goal_evaluations.c.created_at,
                )
                .where(
                    goal_evaluations.c.task_id == task_id,
                    goal_evaluations.c.goal_cycle_id.in_(cycle_ids),
                    goal_evaluations.c.achieved.is_(True),
                )
                .order_by(
                    goal_evaluations.c.goal_cycle_id,
                    goal_evaluations.c.created_at.desc(),
                    goal_evaluations.c.id.desc(),
                )
            ).mappings().all()
            for evaluation in evaluation_rows:
                latest_evaluation_by_cycle.setdefault(
                    evaluation["goal_cycle_id"],
                    dict(evaluation),
                )
        for cycle in cycle_rows:
            evaluation = latest_evaluation_by_cycle.get(cycle["id"])
            if evaluation is None:
                continue
            evidence_fact_ids = self._visible_fact_refs_from_set(
                evaluation["evidence_refs_json"], visible_fact_ids
            )
            history.append(
                {
                    "goal_cycle_id": cycle["id"],
                    "sequence": cycle["sequence"],
                    "goal": cycle["goal"],
                    "completed_at": cycle["completed_at"],
                    "completion_reason": evaluation["reason"] or "",
                    "completion_evaluator": evaluation["evaluator"] or "",
                    "completion_evaluation_id": evaluation["id"],
                    "completion_reason_run_id": evaluation["reason_run_id"],
                    "evidence_fact_ids": evidence_fact_ids,
                }
            )
        return TaskGraph(
            facts=[Fact.model_validate(row) for row in fact_rows],
            intents=[
                self._intent(
                    conn,
                    row,
                    visible_relations_only=True,
                    sources=sources_by_intent.get(row["id"], []),
                    visible_fact_ids=visible_fact_ids,
                )
                for row in intent_rows
            ],
            current_goal_cycle_id=current_goal_cycle_id,
            goal_status=goal_status,
            goal_completed_at=(
                latest_goal_evaluation["created_at"]
                if latest_goal_evaluation is not None
                else None
            ),
            goal_completion_reason=(
                latest_goal_evaluation["reason"]
                if latest_goal_evaluation is not None
                else ""
            ),
            goal_completion_evaluator=(
                latest_goal_evaluation["evaluator"]
                if latest_goal_evaluation is not None
                else ""
            ),
            goal_completion_evaluation_id=(
                latest_goal_evaluation["id"]
                if latest_goal_evaluation is not None
                else None
            ),
            goal_completion_reason_run_id=(
                latest_goal_evaluation["reason_run_id"]
                if latest_goal_evaluation is not None
                else None
            ),
            # Kept as a compatibility field for older consumers.  New graph
            # payloads never derive completion edges from Fact evidence.
            goal_from_fact_ids=[],
            goal_history=history,
        )

    def counts(self, conn: Connection, task_id: str) -> GraphCounts:
        """轻量计数：图谱走骨架/按需加载后，头部「N 事实 / M 意图」不再依赖完整数组。"""
        fact_total = conn.execute(
            select(func.count())
            .select_from(facts)
            .where(facts.c.task_id == task_id, facts.c.archived.is_(False))
        ).scalar_one()
        intent_total = conn.execute(
            select(func.count())
            .select_from(intents)
            .where(intents.c.task_id == task_id, intents.c.archived.is_(False))
        ).scalar_one()
        return GraphCounts(facts=int(fact_total), intents=int(intent_total))

    def children(
        self,
        conn: Connection,
        task_id: str,
        node_id: str,
        *,
        limit: int = 100,
    ) -> GraphChildren:
        """按需展开：返回 node_id 的直接下游 —— 消费它的意图，以及这些意图产出的事实。

        与 graph() 全量路径共用同一套装配（_intent + Fact），保证前端「按需展开拼出来的图」
        与「全量加载的图」逐元素一致。
        """
        source_rows = conn.execute(
            select(intent_sources.c.intent_id)
            .where(
                intent_sources.c.task_id == task_id,
                intent_sources.c.fact_id == node_id,
                intent_sources.c.intent_id.in_(
                    select(intents.c.id).where(
                        intents.c.task_id == task_id,
                        intents.c.archived.is_(False),
                    )
                ),
            )
            .order_by(intent_sources.c.intent_id)
            .limit(limit + 1)
        ).mappings().all()
        intent_ids = [row["intent_id"] for row in source_rows]
        has_more = len(intent_ids) > limit
        intent_ids = intent_ids[:limit]
        if not intent_ids:
            return GraphChildren(node_id=node_id)
        intent_rows = conn.execute(
            select(intents)
            .where(
                intents.c.task_id == task_id,
                intents.c.archived.is_(False),
                intents.c.id.in_(intent_ids),
            )
            .order_by(intents.c.created_at, intents.c.id)
        ).mappings().all()
        produced_ids = {row["to_fact_id"] for row in intent_rows if row["to_fact_id"]}
        fact_rows = []
        if produced_ids:
            fact_rows = conn.execute(
                select(facts)
                .where(
                    facts.c.task_id == task_id,
                    facts.c.archived.is_(False),
                    facts.c.id.in_(produced_ids),
                )
                .order_by(facts.c.created_at, facts.c.id)
            ).mappings().all()
        return GraphChildren(
            node_id=node_id,
            facts=[Fact.model_validate(row) for row in fact_rows],
            intents=[self._intent(conn, row) for row in intent_rows],
            child_count=len(intent_rows),
            has_more=has_more,
        )

    @staticmethod
    def _visible_fact_refs(conn: Connection, task_id: str, refs_json: str) -> list[str]:
        candidate_fact_ids = list(
            dict.fromkeys(
                item_id
                for ref in json.loads(refs_json or "[]")
                for kind, separator, item_id in [ref.partition(":")]
                if separator and kind == "fact" and item_id not in {"origin", "goal"}
            )
        )
        if not candidate_fact_ids:
            return []
        visible = set(
            conn.execute(
                select(facts.c.id).where(
                    facts.c.task_id == task_id,
                    facts.c.id.in_(candidate_fact_ids),
                    facts.c.archived.is_(False),
                )
            ).scalars()
        )
        return [fact_id for fact_id in candidate_fact_ids if fact_id in visible]

    @staticmethod
    def _visible_fact_refs_from_set(
        refs_json: str, visible_fact_ids: set[str]
    ) -> list[str]:
        candidate_fact_ids = list(
            dict.fromkeys(
                item_id
                for ref in json.loads(refs_json or "[]")
                for kind, separator, item_id in [ref.partition(":")]
                if separator and kind == "fact" and item_id not in {"origin", "goal"}
            )
        )
        return [fact_id for fact_id in candidate_fact_ids if fact_id in visible_fact_ids]

    @staticmethod
    def _intent(
        conn: Connection | None,
        row,
        *,
        visible_relations_only: bool = False,
        sources: list[str] | None = None,
        visible_fact_ids: set[str] | None = None,
    ) -> Intent:
        if sources is None:
            source_query = (
                select(intent_sources.c.fact_id)
                .where(
                    intent_sources.c.task_id == row["task_id"],
                    intent_sources.c.intent_id == row["id"],
                )
                .order_by(intent_sources.c.fact_id)
            )
            if visible_relations_only:
                source_query = source_query.join(
                    facts,
                    (facts.c.task_id == intent_sources.c.task_id)
                    & (facts.c.id == intent_sources.c.fact_id),
                ).where(facts.c.archived.is_(False))
            sources = list(conn.execute(source_query).scalars())
        else:
            sources = list(sources)
        to_fact_id = row["to_fact_id"]
        if visible_relations_only and to_fact_id is not None:
            if visible_fact_ids is not None:
                visible_output = to_fact_id in visible_fact_ids
            else:
                visible_output = conn.execute(
                    select(facts.c.id).where(
                        facts.c.task_id == row["task_id"],
                        facts.c.id == to_fact_id,
                        facts.c.archived.is_(False),
                    )
                ).scalar_one_or_none() is not None
            if not visible_output:
                to_fact_id = None
        return Intent(
            id=row["id"],
            task_id=row["task_id"],
            todo_id=row["todo_id"],
            chain_id=row.get("chain_id"),
            kind=row["kind"],
            from_facts=sources,
            description=row["description"],
            priority=50 if row.get("priority") is None else int(row["priority"]),
            status=row["status"],
            creator=row["creator"],
            worker=row["worker"],
            worker_pool=json.loads(row["worker_pool_json"] or "[]"),
            targets=json.loads(row["targets_json"] or "[]"),
            dedup_fingerprint=row.get("dedup_fingerprint") or "",
            plan_metadata=_json_object(row.get("plan_metadata_json")),
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
            to_fact_id=to_fact_id,
            human_intervention_required=bool(row["human_intervention_required"]),
            human_intervention_reason=row["human_intervention_reason"] or "",
            human_intervention_marked_at=row["human_intervention_marked_at"],
            assessment_status=row.get("assessment_status", "open") or "open",
            assessment_reason=row.get("assessment_reason", "") or "",
            assessed_at=row.get("assessed_at"),
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
                intents.c.archived.is_(False),
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
    def restore_paused_intents(conn: Connection, task_id: str) -> int:
        result = conn.execute(
            update(intents)
            .where(
                intents.c.task_id == task_id,
                intents.c.status == "cancelled",
                intents.c.last_failure_kind.in_(["task_stopped", "run_deadline_reached"]),
                intents.c.archived.is_(False),
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

    @staticmethod
    def restore_task_stopped_intents(conn: Connection, task_id: str) -> int:
        """Backward-compatible alias for callers using the old lifecycle name."""

        return GraphRepository.restore_paused_intents(conn, task_id)
