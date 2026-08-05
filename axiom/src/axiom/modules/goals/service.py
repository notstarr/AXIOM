from __future__ import annotations

from sqlalchemy import Engine, select, update

from axiom.modules.artifacts.tables import artifacts
from axiom.modules.goals.repository import GoalRepository
from axiom.modules.goals.schemas import AssessGoal, GoalCycle, GoalEvaluation
from axiom.modules.graph.tables import facts, intents
from axiom.modules.reason.repository import ReasonRepository
from axiom.modules.reason.tables import reason_runs
from axiom.modules.tasks.repository import TaskRepository
from axiom.modules.tasks.tables import task_hints, tasks
from axiom.modules.todos.tables import todos
from axiom.modules.vulnerabilities.tables import vulnerabilities
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
        completion_fact_refs = 0
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
            if kind == "fact":
                completion_fact_refs += 1
        if achieved and completion_fact_refs == 0:
            raise ConflictError("An achieved Goal requires at least one Fact evidence ref")
