from __future__ import annotations

import json

from sqlalchemy import Connection, func, select, update

from axiom.modules.goals.schemas import GoalAssessment, GoalCycle, GoalEvaluation
from axiom.modules.goals.tables import goal_evaluations, task_goal_cycles
from axiom.shared.ids import allocate_id


class GoalRepository:
    def create_cycle(
        self,
        conn: Connection,
        *,
        task_id: str,
        goal: str,
        continuation_reason: str,
        focus: str,
        actor: str,
        now: str,
    ) -> GoalCycle:
        cycle_id = allocate_id(conn, scope="global", kind="goal_cycle", prefix="gc")
        sequence = int(
            conn.execute(
                select(func.coalesce(func.max(task_goal_cycles.c.sequence), 0)).where(
                    task_goal_cycles.c.task_id == task_id
                )
            ).scalar_one()
        ) + 1
        values = {
            "id": cycle_id,
            "task_id": task_id,
            "sequence": sequence,
            "goal": goal,
            "status": "active",
            "continuation_reason": continuation_reason,
            "focus": focus,
            "actor": actor,
            "started_at": now,
            "completed_at": None,
        }
        conn.execute(task_goal_cycles.insert().values(**values))
        return GoalCycle.model_validate(values)

    def get_cycle(self, conn: Connection, task_id: str, cycle_id: str) -> GoalCycle | None:
        row = conn.execute(
            select(task_goal_cycles).where(
                task_goal_cycles.c.task_id == task_id,
                task_goal_cycles.c.id == cycle_id,
            )
        ).mappings().one_or_none()
        return GoalCycle.model_validate(row) if row is not None else None

    def list_cycles(self, conn: Connection, task_id: str) -> list[GoalCycle]:
        rows = conn.execute(
            select(task_goal_cycles)
            .where(task_goal_cycles.c.task_id == task_id)
            .order_by(task_goal_cycles.c.sequence.desc())
        ).mappings()
        return [GoalCycle.model_validate(row) for row in rows]

    @staticmethod
    def complete_cycle(conn: Connection, cycle_id: str, *, now: str) -> None:
        conn.execute(
            update(task_goal_cycles)
            .where(task_goal_cycles.c.id == cycle_id, task_goal_cycles.c.status == "active")
            .values(status="completed", completed_at=now)
        )

    def create(
        self,
        conn: Connection,
        *,
        task_id: str,
        goal_cycle_id: str | None = None,
        assessment: GoalAssessment,
        evaluator: str,
        now: str,
        intent_id: str | None = None,
        reason_run_id: str | None = None,
        workspace_id: str | None = None,
    ) -> GoalEvaluation:
        values = {
            # goal_evaluations.id is the table's global primary key, unlike
            # task-local Fact/Todo/Intent identifiers which use composite
            # primary keys.  Allocate from the global sequence accordingly.
            "id": allocate_id(
                conn,
                scope="global",
                kind="goal_evaluation",
                prefix="ge",
            ),
            "task_id": task_id,
            "goal_cycle_id": goal_cycle_id,
            "intent_id": intent_id,
            "reason_run_id": reason_run_id,
            "workspace_id": workspace_id,
            "achieved": assessment.achieved,
            "reason": assessment.reason,
            "evidence_refs_json": json.dumps(assessment.evidence_refs, ensure_ascii=False),
            "evaluator": evaluator,
            "created_at": now,
        }
        conn.execute(goal_evaluations.insert().values(**values))
        return self._model(values)

    def list(self, conn: Connection, task_id: str) -> list[GoalEvaluation]:
        rows = conn.execute(
            select(goal_evaluations)
            .where(goal_evaluations.c.task_id == task_id)
            .order_by(goal_evaluations.c.created_at.desc(), goal_evaluations.c.id.desc())
        ).mappings()
        return [self._model(row) for row in rows]

    @staticmethod
    def _model(row) -> GoalEvaluation:
        return GoalEvaluation(
            id=row["id"],
            task_id=row["task_id"],
            goal_cycle_id=row.get("goal_cycle_id"),
            intent_id=row["intent_id"],
            reason_run_id=row.get("reason_run_id"),
            workspace_id=row["workspace_id"],
            achieved=bool(row["achieved"]),
            reason=row["reason"],
            evidence_refs=json.loads(row["evidence_refs_json"] or "[]"),
            evaluator=row["evaluator"],
            created_at=row["created_at"],
        )
