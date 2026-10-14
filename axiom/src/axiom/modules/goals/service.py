from __future__ import annotations

import json
from sqlalchemy import Engine, select, update

from axiom.modules.artifacts.tables import artifacts
from axiom.modules.goals.repository import GoalRepository
from axiom.modules.goals.schemas import AssessGoal, GoalCycle, GoalEvaluation
from axiom.modules.graph.tables import facts, intents
from axiom.modules.reason.repository import ReasonRepository
from axiom.modules.reason.tables import reason_runs
from axiom.modules.plan.repository import PlanRepository
from axiom.modules.tasks.repository import TaskRepository
from axiom.modules.tasks.tables import task_hints, tasks
from axiom.modules.todos.tables import todos
from axiom.modules.vulnerabilities.tables import vulnerabilities
from axiom.modules.redteam.tables import red_team_chains, red_team_results
from axiom.shared.clock import utcnow
from axiom.shared.database import transaction
from axiom.shared.errors import ConflictError, NotFoundError


class GoalService:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.repository = GoalRepository()
        self.tasks = TaskRepository()

    def list(self, task_id: str) -> list[GoalEvaluation]:
        with self.engine.connect() as conn:
            task = self.tasks.get(conn, task_id)
            if task is None:
                raise NotFoundError(f"Task not found: {task_id}")
            if task.archived:
                raise ConflictError("已归档任务的目标记录不可见，请先恢复任务")
            return self.repository.list(conn, task_id)

    def list_cycles(self, task_id: str) -> list[GoalCycle]:
        with self.engine.connect() as conn:
            task = self.tasks.get(conn, task_id)
            if task is None:
                raise NotFoundError(f"Task not found: {task_id}")
            if task.archived:
                raise ConflictError("已归档任务的目标记录不可见，请先恢复任务")
            return self.repository.list_cycles(conn, task_id)

    def assess(self, task_id: str, command: AssessGoal) -> GoalEvaluation:
        with transaction(self.engine) as conn:
            task = self.tasks.get(conn, task_id)
            if task is None:
                raise NotFoundError(f"Task not found: {task_id}")
            if task.legacy_readonly:
                raise ConflictError("Legacy Task is read-only")
            if task.archived:
                raise ConflictError("已归档任务不能提交目标评估，请先恢复任务")
            self.validate_evidence(
                conn,
                task_id,
                command.evidence_refs,
                achieved=command.achieved,
            )
            if command.achieved and task.engagement_mode == "red_team":
                self._validate_red_team_completion(conn, task_id, task.red_team_objective)
            now = utcnow()
            evaluation = self.repository.create(
                conn,
                task_id=task_id,
                goal_cycle_id=task.current_goal_cycle_id,
                assessment=command,
                evaluator=command.evaluator,
                now=now,
            )
            if command.achieved:
                self.complete_task(
                    conn,
                    task_id,
                    now=now,
                    goal_cycle_id=task.current_goal_cycle_id,
                )
            return evaluation

    @staticmethod
    def _json_list(value) -> set[str]:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                value = []
        return {str(item) for item in (value or []) if str(item).strip()}

    def _validate_red_team_completion(self, conn, task_id: str, objective) -> None:
        if objective is None:
            raise ConflictError("Red-team task has no objective")
        chains = conn.execute(select(red_team_chains).where(red_team_chains.c.task_id == task_id)).mappings().all()
        results = conn.execute(select(red_team_results).where(red_team_results.c.task_id == task_id)).mappings().all()
        if not chains or not results:
            raise ConflictError("Red-team Goal requires at least one Chain and Result")
        if any(row["status"] == "reviewing" for row in results):
            raise ConflictError("Red-team Goal has Results awaiting review")
        verified = [row for row in results if row["status"] == "verified"]
        if not verified:
            raise ConflictError("Red-team Goal requires verified Results")
        # 积分门槛：verified 成果核定分之和须达到目标分（默认 100）。
        total_points = sum(int(row.get("points_awarded") or 0) for row in verified)
        target_points = int(getattr(objective, "target_points", 100) or 100)
        if total_points < target_points:
            raise ConflictError(
                f"Red-team Goal has {total_points} verified points, target is {target_points}"
            )
        gained = set()
        assets = set()
        for row in verified:
            gained |= self._json_list(row.get("capabilities_gained_json"))
            if row.get("target_asset"):
                assets.add(row["target_asset"])
            for ref in self._json_list(row.get("evidence_refs_json")):
                self.validate_evidence(conn, task_id, [ref], achieved=True)
        target_caps = set(objective.target_capabilities)
        target_assets = set(objective.target_assets)
        if not target_caps.issubset(gained):
            raise ConflictError("Red-team Goal has not reached all target capabilities")
        if not target_assets.issubset(assets):
            raise ConflictError("Red-team Goal has not reached all target assets")
        for target in objective.data_targets:
            matches = [row for row in verified if row.get("result_type") == "data" and row.get("data_target_id") == target.id]
            if not matches:
                raise ConflictError(f"Missing verified data Result for target: {target.id}")
            if any((row.get("sample_count") or 0) > target.max_records or (row.get("sample_bytes") or 0) > target.max_bytes for row in matches):
                raise ConflictError(f"Data Result exceeds objective limits: {target.id}")

    @staticmethod
    def complete_task(
        conn,
        task_id: str,
        *,
        now: str,
        exclude_intent_id: str | None = None,
        exclude_workspace_id: str | None = None,
        goal_cycle_id: str | None = None,
    ) -> None:
        open_intents = intents.c.status.in_(["pending", "claimed", "running", "concluding"])
        condition = intents.c.task_id == task_id
        if exclude_intent_id:
            condition = condition & (intents.c.id != exclude_intent_id)
        conn.execute(
            update(intents)
            .where(condition, open_intents, intents.c.archived.is_(False))
            .values(
                status="cancelled",
                worker=None,
                lease_id=None,
                claimed_at=None,
                last_heartbeat_at=None,
                next_retry_at=None,
                concluded_at=now,
                last_failure_kind="goal_achieved",
                failure_class="",
            )
        )
        conn.execute(
            update(reason_runs)
            .where(
                reason_runs.c.task_id == task_id,
                reason_runs.c.status.in_(["pending", "claimed", "running", "concluding"]),
            )
            .values(
                status="cancelled",
                worker=None,
                lease_id=None,
                claimed_at=None,
                last_heartbeat_at=None,
                next_retry_at=None,
                last_failure_kind="goal_achieved",
                failure_class="",
                activity_kind="cancelled",
                activity_summary="Goal 已达成，ReasonRun 已取消",
                activity_tool="",
                activity_updated_at=now,
                updated_at=now,
                concluded_at=now,
            )
        )
        ReasonRepository.abandon_open(
            conn,
            task_id=task_id,
            failure_reason="goal_achieved",
            now=now,
            exclude_workspace_id=exclude_workspace_id,
        )
        # Plan is an independent planning runtime.  Completing the Goal must
        # invalidate its queued/leased runs as well, otherwise a manual Goal
        # decision could leave a Plan Worker running after the Task is done.
        PlanRepository.cancel_open_for_task(
            conn,
            task_id,
            now=now,
            failure_kind="goal_achieved",
        )
        PlanRepository.abandon_open(
            conn,
            task_id=task_id,
            failure_reason="goal_achieved",
            now=now,
        )
        if goal_cycle_id is None:
            goal_cycle_id = conn.execute(
                select(tasks.c.current_goal_cycle_id).where(tasks.c.id == task_id)
            ).scalar_one_or_none()
        if goal_cycle_id:
            GoalRepository.complete_cycle(conn, goal_cycle_id, now=now)
        conn.execute(
            update(tasks)
            .where(tasks.c.id == task_id)
            .values(status="completed", phase="none", completed_at=now, updated_at=now)
        )

    @staticmethod
    def validate_evidence(
        conn,
        task_id: str,
        refs: list[str],
        *,
        achieved: bool = False,
    ) -> None:
        tables = {
            "fact": (facts, facts.c.id, facts.c.archived),
            "artifact": (artifacts, artifacts.c.id, artifacts.c.archived),
            "vulnerability": (
                vulnerabilities,
                vulnerabilities.c.id,
                vulnerabilities.c.archived,
            ),
            "todo": (todos, todos.c.id, todos.c.archived),
            "hint": (task_hints, task_hints.c.id, task_hints.c.archived),
        }
        for ref in refs:
            kind, separator, item_id = ref.partition(":")
            if not separator or kind not in tables:
                raise ConflictError(f"Unsupported Goal evidence ref: {ref}")
            if kind == "fact" and item_id == "goal":
                raise ConflictError("Goal Fact cannot be used as Goal evidence")
            table, id_column, archived_column = tables[kind]
            exists = conn.execute(
                select(id_column).where(
                    table.c.task_id == task_id,
                    id_column == item_id,
                    archived_column.is_(False),
                )
            ).scalar_one_or_none()
            if exists is None:
                raise ConflictError(f"Goal evidence not found: {ref}")
