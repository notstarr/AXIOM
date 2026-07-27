from __future__ import annotations

import json

from sqlalchemy import Connection, select

from axiom.modules.goals.schemas import GoalAssessment, GoalEvaluation
from axiom.modules.goals.tables import goal_evaluations
from axiom.shared.ids import allocate_id


class GoalRepository:
    def create(
        self,
        conn: Connection,
        *,
        task_id: str,
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
            intent_id=row["intent_id"],
            reason_run_id=row.get("reason_run_id"),
            workspace_id=row["workspace_id"],
            achieved=bool(row["achieved"]),
            reason=row["reason"],
            evidence_refs=json.loads(row["evidence_refs_json"] or "[]"),
            evaluator=row["evaluator"],
            created_at=row["created_at"],
        )
