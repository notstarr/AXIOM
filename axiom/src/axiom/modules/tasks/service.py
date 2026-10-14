from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import (
    Engine,
    cast,
    delete as sql_delete,
    func,
    literal,
    select,
    union_all,
    update,
)

from axiom.modules.audit.schemas import RecordAuditEvent
from axiom.modules.audit.service import AuditService
from axiom.modules.artifacts.tables import artifacts
from axiom.modules.graph.repository import GraphRepository
from axiom.modules.graph.tables import facts, intents
from axiom.modules.goals.repository import GoalRepository
from axiom.modules.goals.tables import task_goal_cycles
from axiom.modules.plan.tables import plan_runs
from axiom.modules.plan.repository import PlanRepository
from axiom.modules.projects.repository import ProjectRepository
from axiom.modules.reason.repository import ReasonRepository
from axiom.modules.reason.service import ReasonService
from axiom.modules.reason.tables import reason_runs
from axiom.modules.redteam.schemas import RedTeamObjective, RedTeamPrincipal
from axiom.modules.tasks.repository import TaskRepository
from axiom.modules.tasks.schemas import (
    ActivePlanRun,
    ActiveReasonRun,
    ContinueTask,
    ContinueTaskResult,
    CreateTask,
    CreateTaskHint,
    Origin,
    ResumeTask,
    RunDeadlineReconcileReport,
    Task,
    TaskHint,
    TaskScope,
    UpdateTask,
    UpdateTaskHint,
)
from axiom.modules.tasks.tables import task_hints, tasks
from axiom.modules.todos.tables import todos
from axiom.modules.vulnerabilities.tables import vulnerabilities
from axiom.modules.vulnerability_reviews.tables import vulnerability_review_runs
from axiom.modules.sandboxes.service import TaskSandboxManager
from axiom.shared import model_registry as _model_registry  # noqa: F401
from axiom.shared.clock import format_utc, parse_utc, utcnow
from axiom.shared.database import metadata, run_transaction, transaction
from axiom.shared.errors import ConflictError, NotFoundError
from axiom.shared.ids import id_counters
from axiom.shared.storage import FileStore, PreparedTaskDeletion


def _json_object(value: object) -> dict[str, object]:
    """Decode a JSON object column without letting malformed history break Task API."""

    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value:
        return {}
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


class TaskService:
    def __init__(self, engine: Engine, store: FileStore, *, sandboxes: TaskSandboxManager | None = None):
        self.engine = engine
        self.store = store
        self.projects = ProjectRepository()
        self.tasks = TaskRepository()
        self.graph = GraphRepository()
        self.goals = GoalRepository()
        self.reason = ReasonRepository()
        self.plan = PlanRepository()
        self.reason_service = ReasonService(engine)
        self.audit = AuditService(engine)
        self.sandboxes = sandboxes

    def create(self, command: CreateTask) -> Task:
        origin = Origin(original_input=command.origin)
        scope = command.scope or TaskScope(enabled=False)
        now = utcnow()
        deadline_at = self._deadline_at(now, command.run_duration_seconds)
        # 模式即接口：红队任务未显式声明结构化目标时合成宽松默认——
        # 不设能力/资产/数据上限，Reason 依任务目标自行规划，
        # 由成果审核与 Goal 校验兜底。
        red_team_objective = command.red_team_objective
        if command.engagement_mode == "red_team" and red_team_objective is None:
            red_team_objective = RedTeamObjective(
                initial_principal=RedTeamPrincipal(id="user:initial", label="初始身份"),
            )
        with self.engine.begin() as conn:
            project = self.projects.get(conn, command.project_id)
            if project is None:
                raise NotFoundError(f"Project not found: {command.project_id}")
            if project.status != "active":
                raise ConflictError("Task cannot be created in an archived Project")
            task = self.tasks.create(
                conn,
                project_id=command.project_id,
                title=command.title,
                origin=origin,
                goal=command.goal,
                engagement_mode=command.engagement_mode,
                red_team_objective=red_team_objective,
                scope=scope,
                worker_pool=command.worker_pool,
                guard_enabled=command.guard_enabled,
                run_duration_seconds=command.run_duration_seconds,
                run_started_at=now,
                run_deadline_at=deadline_at,
                hints=command.hints,
                now=now,
            )
            cycle = self.goals.create_cycle(
                conn,
                task_id=task.id,
                goal=task.goal,
                continuation_reason="",
                focus="",
                actor="system.create",
                now=now,
            )
            conn.execute(
                update(tasks)
                .where(tasks.c.id == task.id)
                .values(current_goal_cycle_id=cycle.id)
            )
            task = task.model_copy(update={"current_goal_cycle_id": cycle.id})
            self.graph.seed_task(conn, task=task, now=now)
            self._record_run_started(
                conn,
                task=task,
                source="create",
                duration_seconds=command.run_duration_seconds,
                deadline_at=deadline_at,
            )
        return self._decorate(task)

    def get(self, task_id: str) -> Task:
        with self.engine.connect() as conn:
            task = self.tasks.get(conn, task_id)
        if task is None:
            raise NotFoundError(f"Task not found: {task_id}")
        return self._decorate_many([task])[0]

    def update(self, task_id: str, command: UpdateTask) -> Task:
        with self.engine.begin() as conn:
            task = self.tasks.get(conn, task_id)
            if task is None:
                raise NotFoundError(f"Task not found: {task_id}")
            if task.legacy_readonly:
                raise ConflictError("Legacy Task is read-only")
            if task.archived:
                raise ConflictError("已归档任务不能修改")
            if command.goal is not None and task.status == "completed":
                raise ConflictError("已完成任务必须通过继续任务创建新的 Goal")
            now = utcnow()
            updated = self.tasks.update(conn, task_id, command, now=now)
            if command.goal is not None:
                conn.execute(
                    update(facts)
                    .where(facts.c.task_id == task_id, facts.c.id == "goal")
                    .values(description=command.goal)
                )
                if task.current_goal_cycle_id:
                    conn.execute(
                        update(task_goal_cycles)
                        .where(
                            task_goal_cycles.c.id == task.current_goal_cycle_id,
                            task_goal_cycles.c.status == "active",
                        )
                        .values(goal=command.goal)
                    )
                self.audit.record_in(
                    conn,
                    RecordAuditEvent(
                        project_id=task.project_id,
                        task_id=task.id,
                        event_type="task.goal_updated",
                        message="当前 Goal 已更新",
                        payload={
                            "goal_cycle_id": task.current_goal_cycle_id,
                            "previous_goal": task.goal,
                            "goal": command.goal,
                        },
                    ),
                )
            if command.worker_pool is not None:
                conn.execute(
                    update(intents)
                    .where(
                        intents.c.task_id == task_id,
                        intents.c.status == "pending",
                        intents.c.archived.is_(False),
                    )
                    .values(worker_pool_json=json.dumps(command.worker_pool, ensure_ascii=False))
                )
                conn.execute(
                    update(reason_runs)
                    .where(
                        reason_runs.c.task_id == task_id,
                        reason_runs.c.status == "pending",
                        reason_runs.c.trigger != "plan",
                    )
                    .values(
                        worker_pool_json=json.dumps(command.worker_pool, ensure_ascii=False),
                        updated_at=now,
                    )
                )
            if command.worker_pool is not None:
                # Plan is equivalent to the other phases: pending PlanRuns
                # follow the ordinary task pool.
                conn.execute(
                    update(plan_runs)
                    .where(plan_runs.c.task_id == task_id, plan_runs.c.status == "pending")
                    .values(
                        worker_pool_json=json.dumps(command.worker_pool, ensure_ascii=False),
                        updated_at=now,
                    )
                )
        assert updated is not None
        return self._decorate(updated)

    def continue_task(self, task_id: str, command: ContinueTask) -> ContinueTaskResult:
        now = utcnow()
        deadline_at = self._deadline_at(now, command.run_duration_seconds)

        def continue_in(conn):
            if conn.execute(
                select(tasks.c.id).where(tasks.c.id == task_id).with_for_update()
            ).scalar_one_or_none() is None:
                raise NotFoundError(f"Task not found: {task_id}")
            task = self.tasks.get(conn, task_id)
            assert task is not None
            if task.legacy_readonly:
                raise ConflictError("历史只读任务不能继续运行")
            if task.archived:
                raise ConflictError("已归档任务不能继续运行")
            if task.status != "completed":
                raise ConflictError("只有已完成任务可以继续运行")
            previous_goal = task.goal
            cycle = self.goals.create_cycle(
                conn,
                task_id=task_id,
                goal=command.goal,
                continuation_reason=command.reason,
                focus=command.focus,
                actor=command.actor,
                now=now,
            )
            conn.execute(
                update(tasks)
                .where(tasks.c.id == task_id, tasks.c.status == "completed")
                .values(
                    goal=command.goal,
                    current_goal_cycle_id=cycle.id,
                    status="running",
                    phase="reason",
                    completed_at=None,
                    run_duration_seconds=command.run_duration_seconds,
                    run_started_at=now,
                    run_deadline_at=deadline_at,
                    last_pause_reason=None,
                    updated_at=now,
                )
            )
            conn.execute(
                update(facts)
                .where(facts.c.task_id == task_id, facts.c.id == "goal")
                .values(description=command.goal)
            )
            continued = self.tasks.get(conn, task_id)
            assert continued is not None
            reason_run = self.reason_service.create_continuation_in(
                conn,
                task=continued,
                previous_goal=previous_goal,
                continuation_reason=command.reason,
                focus=command.focus,
                actor=command.actor,
                now=now,
            )
            self._record_run_started(
                conn,
                task=continued,
                source="continuation",
                duration_seconds=command.run_duration_seconds,
                deadline_at=deadline_at,
            )
            self.audit.record_in(
                conn,
                RecordAuditEvent(
                    project_id=continued.project_id,
                    task_id=task_id,
                    reason_run_id=reason_run.id,
                    event_type="task.continued",
                    message=f"任务已进入 Goal 第 {cycle.sequence} 轮",
                    payload={
                        "goal_cycle_id": cycle.id,
                        "sequence": cycle.sequence,
                        "previous_goal": previous_goal,
                        "goal": command.goal,
                        "reason": command.reason,
                        "focus": command.focus,
                        "actor": command.actor,
                    },
                ),
            )
            return continued, cycle, reason_run

        continued, cycle, reason_run = run_transaction(self.engine, continue_in)
        return ContinueTaskResult(
            task=self._decorate(continued),
            goal_cycle=cycle,
            reason_run_id=reason_run.id,
        )

    def list(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        phase: str | None = None,
        search: str = "",
    ) -> list[Task]:
        with self.engine.connect() as conn:
            result = self.tasks.list(
                conn,
                project_id=project_id,
                status=status,
                phase=phase,
                search=search,
            )
        return self._decorate_many(result)

    def stop(self, task_id: str) -> Task:
        with self.engine.begin() as conn:
            task = self.tasks.get(conn, task_id)
            if task is None:
                raise NotFoundError(f"Task not found: {task_id}")
            if task.status != "running":
                raise ConflictError("Only a running Task can be stopped")
            if task.archived:
                raise ConflictError("已归档任务不能暂停")
            now = utcnow()
            stopped = self._pause_in(
                conn,
                task=task,
                pause_reason="manual",
                failure_kind="task_stopped",
                now=now,
            )
            assert stopped is not None
        return self._decorate(stopped)

    def resume(self, task_id: str, command: ResumeTask | None = None) -> Task:
        duration_seconds = command.run_duration_seconds if command is not None else None
        now = utcnow()
        deadline_at = self._deadline_at(now, duration_seconds)
        with self.engine.begin() as conn:
            task = self.tasks.get(conn, task_id)
            if task is None:
                raise NotFoundError(f"Task not found: {task_id}")
            if task.status != "stopped":
                raise ConflictError("Only a stopped Task can be resumed")
            if task.archived:
                raise ConflictError("已归档任务不能恢复运行")
            self.graph.restore_paused_intents(conn, task_id)
            bootstrap = self.graph.get_intent(conn, task_id, "i001")
            phase = "waiting" if bootstrap and bootstrap.status == "completed" else "bootstrap"
            resumed = self.tasks.resume_with_window(
                conn,
                task_id,
                phase=phase,
                run_duration_seconds=duration_seconds,
                run_started_at=now,
                run_deadline_at=deadline_at,
                now=now,
            )
            assert resumed is not None
            self._record_run_started(
                conn,
                task=resumed,
                source="resume",
                duration_seconds=duration_seconds,
                deadline_at=deadline_at,
            )
        return self._decorate(resumed)

    def pause_manual(self, task_id: str) -> Task:
        """人工暂停运行中的任务（钉钉指令/人工操作入口）。"""
        now = utcnow()
        with self.engine.begin() as conn:
            task = self.tasks.get(conn, task_id)
            if task is None:
                raise NotFoundError(f"Task not found: {task_id}")
            if task.status != "running":
                raise ConflictError("Only a running Task can be paused")
            stopped = self._pause_in(
                conn,
                task=task,
                pause_reason="manual",
                failure_kind="manual",
                now=now,
            )
            if stopped is None:
                raise ConflictError("Task is not running")
            return stopped

    def reconcile_run_deadlines(self) -> RunDeadlineReconcileReport:
        checked_at = utcnow()
        with self.engine.connect() as conn:
            candidates = self.tasks.expired_running_ids(conn, now=checked_at)
        paused_task_ids: list[str] = []
        for task_id in candidates:
            with transaction(self.engine) as conn:
                task = self.tasks.get(conn, task_id)
                if task is None:
                    continue
                stopped = self._pause_in(
                    conn,
                    task=task,
                    pause_reason="run_deadline_reached",
                    failure_kind="run_deadline_reached",
                    now=checked_at,
                    only_if_due=True,
                )
                if stopped is not None:
                    paused_task_ids.append(task_id)
        return RunDeadlineReconcileReport(
            checked_at=checked_at,
            paused_task_ids=paused_task_ids,
        )

    def delete(self, task_id: str) -> None:
        prepared: PreparedTaskDeletion | None = None
        try:
            with self.engine.begin() as conn:
                locked_task_id = conn.execute(
                    select(tasks.c.id).where(tasks.c.id == task_id).with_for_update()
                ).scalar_one_or_none()
                if locked_task_id is None:
                    raise NotFoundError(f"Task not found: {task_id}")
                task = self.tasks.get(conn, task_id)
                assert task is not None
                if task.legacy_readonly:
                    raise ConflictError("历史只读任务不能永久删除")
                if not task.archived:
                    raise ConflictError("请先归档任务，再永久删除")
                active_count = sum(
                    conn.execute(
                        select(func.count())
                        .select_from(table)
                        .where(
                            table.c.task_id == task_id,
                            table.c.status.in_(("claimed", "running", "concluding")),
                        )
                    ).scalar_one()
                    for table in (intents, reason_runs, plan_runs, vulnerability_review_runs)
                )
                if active_count:
                    raise ConflictError("任务仍有活动 Worker Lease，暂时不能永久删除")

                # An archived task should already have released its sandbox,
                # but keep deletion safe for databases created before the
                # sandbox lifecycle hook was introduced.
                if self.sandboxes is not None:
                    sandbox = self.sandboxes.get(task_id)
                    if sandbox is not None and sandbox.status != "terminated":
                        raise ConflictError("任务 Sandbox 尚未销毁，暂时不能永久删除")

                prepared = self.store.prepare_task_deletion(task_id)
                for table in reversed(metadata.sorted_tables):
                    if table is tasks or "task_id" not in table.c:
                        continue
                    conn.execute(sql_delete(table).where(table.c.task_id == task_id))
                conn.execute(sql_delete(id_counters).where(id_counters.c.scope == task_id))
                deleted = self.tasks.delete(conn, task_id)
                if not deleted:
                    raise NotFoundError(f"Task not found: {task_id}")
        except BaseException:
            if prepared is not None:
                prepared.rollback()
            raise
        else:
            assert prepared is not None
            prepared.finish()

    def archive(self, task_id: str, *, actor: str = "human.frontend") -> Task:
        actor = actor.strip() or "human.frontend"
        now = utcnow()
        with transaction(self.engine) as conn:
            task = self.tasks.get(conn, task_id)
            if task is None:
                raise NotFoundError(f"Task not found: {task_id}")
            if task.legacy_readonly:
                raise ConflictError("历史只读任务不能归档")
            if task.archived:
                raise ConflictError("任务已经归档")
            if task.status == "running":
                raise ConflictError("请先暂停任务，再归档")
            # Archived Tasks are never schedulable again until explicitly
            # unarchived.  Cancel queued Explore work before releasing the
            # sandbox so no pending run can keep the writable environment
            # alive indefinitely.
            conn.execute(
                update(intents)
                .where(
                    intents.c.task_id == task_id,
                    intents.c.status == "pending",
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
                    last_failure_kind="task_archived",
                    activity_kind="cancelled",
                    activity_summary="任务已归档，排队中的 Explore 已取消",
                    activity_updated_at=now,
                )
            )
            self.reason.cancel_open_for_task(
                conn, task_id, now=now, failure_kind="task_archived"
            )
            self.plan.cancel_open_for_task(
                conn, task_id, now=now, failure_kind="task_archived"
            )
            active_count = sum(
                conn.execute(
                    select(func.count())
                    .select_from(table)
                    .where(
                        table.c.task_id == task_id,
                        table.c.status.in_(("claimed", "running", "concluding")),
                    )
                ).scalar_one()
                for table in (intents, reason_runs, plan_runs, vulnerability_review_runs)
            )
            if active_count:
                raise ConflictError("任务仍有活动 Worker Lease，暂时不能归档")
            updated = conn.execute(
                update(tasks)
                .where(tasks.c.id == task_id, tasks.c.archived.is_(False))
                .values(archived=True, updated_at=now)
            )
            if not updated.rowcount:
                raise ConflictError("任务归档状态已被其他操作改变")
            self.audit.record_in(
                conn,
                RecordAuditEvent(
                    project_id=task.project_id,
                    task_id=task_id,
                    event_type="task.archived",
                    message="任务已归档，不再进入 Worker 调度",
                    payload={"actor": actor, "status": task.status},
                ),
            )
            archived = self.tasks.get(conn, task_id)
            assert archived is not None
        if self.sandboxes is not None:
            destroyed = self.sandboxes.destroy_if_idle(task_id)
            if destroyed:
                with transaction(self.engine) as conn:
                    self.audit.record_in(
                        conn,
                        RecordAuditEvent(
                            project_id=archived.project_id,
                            task_id=task_id,
                            event_type="sandbox.destroyed",
                            message="任务归档后已销毁 Task Sandbox",
                            payload={"reason": "task_archived"},
                        ),
                    )
        return self._decorate(archived)

    def unarchive(self, task_id: str, *, actor: str = "human.frontend") -> Task:
        actor = actor.strip() or "human.frontend"
        now = utcnow()
        with transaction(self.engine) as conn:
            task = self.tasks.get(conn, task_id)
            if task is None:
                raise NotFoundError(f"Task not found: {task_id}")
            if task.legacy_readonly:
                raise ConflictError("历史只读任务不能恢复归档")
            if not task.archived:
                raise ConflictError("任务当前未归档")
            updated = conn.execute(
                update(tasks)
                .where(tasks.c.id == task_id, tasks.c.archived.is_(True))
                .values(archived=False, updated_at=now)
            )
            if not updated.rowcount:
                raise ConflictError("任务归档状态已被其他操作改变")
            self.audit.record_in(
                conn,
                RecordAuditEvent(
                    project_id=task.project_id,
                    task_id=task_id,
                    event_type="task.unarchived",
                    message="任务已恢复可见",
                    payload={"actor": actor, "status": task.status},
                ),
            )
            restored = self.tasks.get(conn, task_id)
            assert restored is not None
        return self._decorate(restored)

    def hints(self, task_id: str) -> list[TaskHint]:
        with self.engine.connect() as conn:
            task = self.tasks.get(conn, task_id)
            if task is None:
                raise NotFoundError(f"Task not found: {task_id}")
            if task.archived:
                raise ConflictError("已归档任务的 Hint 不可见，请先恢复任务")
            return self.tasks.hints(conn, task_id)

    def add_hint(self, task_id: str, command: CreateTaskHint) -> TaskHint:
        with self.engine.begin() as conn:
            task_lock = conn.execute(
                select(tasks.c.id).where(tasks.c.id == task_id).with_for_update()
            ).scalar_one_or_none()
            if task_lock is None:
                raise NotFoundError(f"Task not found: {task_id}")
            task = self.tasks.get(conn, task_id)
            assert task is not None
            if task.legacy_readonly:
                raise ConflictError("Legacy Task is read-only")
            if task.archived:
                raise ConflictError("已归档任务不能修改 Hint")
            now = utcnow()
            hint = self.tasks.create_hint(conn, task_id=task_id, command=command, now=now)
            self.reason_service.trigger_hint_in(
                conn,
                task_id=task_id,
                hint=hint,
                now=now,
            )
            return hint

    def update_hint(self, task_id: str, hint_id: str, command: UpdateTaskHint) -> TaskHint:
        with self.engine.begin() as conn:
            task = self.tasks.get(conn, task_id)
            if task is None:
                raise NotFoundError(f"Task not found: {task_id}")
            if task.legacy_readonly:
                raise ConflictError("Legacy Task is read-only")
            if task.archived:
                raise ConflictError("已归档任务不能修改 Hint")
            updated = self.tasks.update_hint(
                conn,
                task_id=task_id,
                hint_id=hint_id,
                command=command,
                now=utcnow(),
            )
        if updated is None:
            raise NotFoundError(f"Hint not found: {hint_id}")
        return updated

    def _pause_in(
        self,
        conn,
        *,
        task: Task,
        pause_reason: str,
        failure_kind: str,
        now: str,
        only_if_due: bool = False,
    ) -> Task | None:
        stopped = self.tasks.pause_running(
            conn,
            task.id,
            reason=pause_reason,
            now=now,
            only_if_due=only_if_due,
        )
        if stopped is None:
            return None
        self.graph.cancel_active_intents(
            conn,
            task.id,
            now=now,
            failure_kind=failure_kind,
        )
        self.reason.abandon_open(
            conn,
            task_id=task.id,
            failure_reason=failure_kind,
            now=now,
        )
        self.reason.cancel_open_for_task(
            conn,
            task.id,
            now=now,
            failure_kind=failure_kind,
        )
        self.plan.cancel_open_for_task(
            conn,
            task.id,
            now=now,
            failure_kind=failure_kind,
        )
        self.plan.abandon_open(
            conn,
            task_id=task.id,
            failure_reason=failure_kind,
            now=now,
        )
        self.audit.record_in(
            conn,
            RecordAuditEvent(
                project_id=task.project_id,
                task_id=task.id,
                event_type="task.paused",
                message=(
                    "任务运行时间已到，系统已自动暂停"
                    if pause_reason == "run_deadline_reached"
                    else "任务已由用户暂停"
                ),
                payload={
                    "reason": pause_reason,
                    "run_started_at": task.run_started_at,
                    "run_duration_seconds": task.run_duration_seconds,
                    "run_deadline_at": task.run_deadline_at,
                },
            ),
        )
        return stopped

    def _record_run_started(
        self,
        conn,
        *,
        task: Task,
        source: str,
        duration_seconds: int | None,
        deadline_at: str | None,
    ) -> None:
        self.audit.record_in(
            conn,
            RecordAuditEvent(
                project_id=task.project_id,
                task_id=task.id,
                event_type="task.run_window_started",
                message="任务已开始新的运行窗口",
                payload={
                    "source": source,
                    "run_duration_seconds": duration_seconds,
                    "run_deadline_at": deadline_at,
                },
            ),
        )

    @staticmethod
    def _deadline_at(now: str, duration_seconds: int | None) -> str | None:
        if duration_seconds is None:
            return None
        return format_utc(parse_utc(now) + timedelta(seconds=duration_seconds))

    def _decorate(self, task: Task) -> Task:
        return self._decorate_many([task])[0]

    def _decorate_many(self, tasks_to_decorate: list[Task]) -> list[Task]:
        """Attach counters and active Reason state with a fixed number of bulk queries."""
        if not tasks_to_decorate:
            return []
        task_ids = [task.id for task in tasks_to_decorate]

        def blocked_count_query(table, metric: str):
            """Count work parked after exhausting its infrastructure retries.

            Keyed on the ``retry_blocked`` activity marker the release paths
            write, not on ``infrastructure_retry_count >= limit``: that keeps
            this counter accurate if the limit is later reconfigured, and it
            needs no extra read of the runtime settings.
            """

            return (
                select(
                    table.c.task_id.label("task_id"),
                    literal(metric).label("metric"),
                    func.count().label("count"),
                )
                .where(
                    table.c.task_id.in_(task_ids),
                    table.c.status == "pending",
                    table.c.activity_kind == "retry_blocked",
                )
                .group_by(table.c.task_id)
            )

        def count_query(table, metric: str, *conditions):
            return (
                select(
                    table.c.task_id.label("task_id"),
                    literal(metric).label("metric"),
                    func.count().label("count"),
                )
                .where(
                    table.c.task_id.in_(task_ids),
                    table.c.archived.is_(False),
                    *conditions,
                )
                .group_by(table.c.task_id)
            )

        with self.engine.connect() as conn:
            count_rows = conn.execute(
                union_all(
                    count_query(facts, "facts"),
                    count_query(task_hints, "hints"),
                    count_query(todos, "todos"),
                    count_query(vulnerabilities, "vulnerabilities"),
                    count_query(artifacts, "artifacts"),
                    count_query(intents, "intents"),
                    count_query(
                        intents,
                        "working_intents",
                        intents.c.kind == "explore",
                        intents.c.status.in_(["claimed", "running", "concluding"]),
                    ),
                    # Work parked after exhausting its infrastructure retry
                    # budget.  Only intents carry ``archived``; the run tables
                    # do not, so each gets its own metric and the totals are
                    # summed below.
                    count_query(
                        intents,
                        "blocked_intents",
                        intents.c.status == "pending",
                        intents.c.activity_kind == "retry_blocked",
                    ),
                    blocked_count_query(reason_runs, "blocked_reason_runs"),
                    blocked_count_query(plan_runs, "blocked_plan_runs"),
                    blocked_count_query(vulnerability_review_runs, "blocked_reviews"),
                )
            ).mappings()
            counts: dict[str, dict[str, int]] = {}
            for row in count_rows:
                counts.setdefault(str(row["metric"]), {})[str(row["task_id"])] = int(
                    row["count"] or 0
                )
            # Reason and Plan are separate work types.  Legacy Plan records
            # written to ``reason_runs(trigger='plan')`` remain readable by
            # their old endpoints, but must never be surfaced as Reason work
            # or mixed into the new Plan queue.
            # Reason and Plan have separate tables, but the Task read model
            # only needs their shared lifecycle fields. Fetch both branches in
            # one UNION so list/detail decoration keeps a fixed query count.
            reason_run_select = select(
                reason_runs.c.id,
                reason_runs.c.task_id,
                reason_runs.c.status,
                reason_runs.c.trigger,
                reason_runs.c.trigger_payload_json,
                reason_runs.c.worker,
                reason_runs.c.lease_id,
                reason_runs.c.settings_version,
                reason_runs.c.execute_timeout_seconds,
                reason_runs.c.conclude_timeout_seconds,
                reason_runs.c.last_heartbeat_at,
                reason_runs.c.activity_kind,
                reason_runs.c.activity_summary,
                reason_runs.c.activity_tool,
                reason_runs.c.activity_started_at,
                reason_runs.c.activity_updated_at,
                reason_runs.c.created_at,
                reason_runs.c.concluded_at,
                cast(literal("{}"), plan_runs.c.decision_json.type).label("decision_json"),
                literal("reason").label("run_kind"),
            ).where(
                reason_runs.c.task_id.in_(task_ids),
                reason_runs.c.trigger != "plan",
                reason_runs.c.status.in_(
                    ["pending", "claimed", "running", "concluding"]
                ),
            )
            plan_run_select = select(
                plan_runs.c.id,
                plan_runs.c.task_id,
                plan_runs.c.status,
                plan_runs.c.trigger,
                plan_runs.c.trigger_payload_json,
                plan_runs.c.worker,
                plan_runs.c.lease_id,
                plan_runs.c.settings_version,
                plan_runs.c.execute_timeout_seconds,
                plan_runs.c.conclude_timeout_seconds,
                plan_runs.c.last_heartbeat_at,
                plan_runs.c.activity_kind,
                plan_runs.c.activity_summary,
                plan_runs.c.activity_tool,
                plan_runs.c.activity_started_at,
                plan_runs.c.activity_updated_at,
                plan_runs.c.created_at,
                plan_runs.c.concluded_at,
                plan_runs.c.decision_json,
                literal("plan").label("run_kind"),
            ).where(
                plan_runs.c.task_id.in_(task_ids),
                plan_runs.c.status.in_(
                    [
                        "pending",
                        "claimed",
                        "running",
                        "concluding",
                        "completed",
                        "failed",
                    ]
                ),
            )
            run_rows = conn.execute(
                union_all(reason_run_select, plan_run_select).order_by(
                    "task_id", "created_at", "id"
                )
            ).mappings().all()
            reasons: dict[str, object] = {}
            queued_reasons: dict[str, list[object]] = {}
            active_plans: dict[str, object] = {}
            queued_plans: dict[str, list[object]] = {}
            plan_counts: dict[str, int] = {}
            latest_plan_decisions: dict[str, dict[str, object]] = {}
            for row in run_rows:
                task_id = str(row["task_id"])
                if row["run_kind"] == "reason":
                    if row["status"] in {"claimed", "running", "concluding"}:
                        reasons.setdefault(task_id, row)
                    elif row["status"] == "pending":
                        queued_reasons.setdefault(task_id, []).append(row)
                    continue
                status = row["status"]
                if status in {"completed", "failed"}:
                    plan_counts[task_id] = plan_counts.get(task_id, 0) + 1
                    decision = _json_object(row["decision_json"])
                    payload = _json_object(row["trigger_payload_json"])
                    # Older Plan implementations may have persisted the
                    # decision in trigger_payload_json.  This fallback is
                    # read-only compatibility and does not change the new
                    # PlanRun storage contract.
                    if not decision:
                        decision = payload
                    previous = latest_plan_decisions.get(task_id)
                    if previous is None or (
                        row["created_at"],
                        row["id"],
                    ) > (
                        previous["_created_at"],
                        previous["_id"],
                    ):
                        latest_plan_decisions[task_id] = {
                            "_created_at": row["created_at"],
                            "_id": row["id"],
                            "plan_run_id": row["id"],
                            "status": status,
                            "decision": decision.get("decision"),
                            "reviewed_dimensions": decision.get("reviewed_dimensions", []),
                            "exhaustion_reason": decision.get("exhaustion_reason", ""),
                            "evidence_refs": decision.get("evidence_refs", []),
                            "confidence": decision.get("confidence", "unspecified"),
                            "concluded_at": row["concluded_at"],
                        }
                elif status in {"claimed", "running", "concluding"}:
                    active_plans.setdefault(task_id, row)
                elif status == "pending":
                    queued_plans.setdefault(task_id, []).append(row)

            for decision in latest_plan_decisions.values():
                decision.pop("_created_at", None)
                decision.pop("_id", None)

        decorated: list[Task] = []
        for task in tasks_to_decorate:
            reason = reasons.get(task.id)
            reason_present = reason is not None
            plan = active_plans.get(task.id)
            plan_present = plan is not None
            task_queued = queued_reasons.get(task.id, [])
            task_plan_queued = queued_plans.get(task.id, [])
            # Keep the legacy Reason activity fields Reason-only.  A PlanRun
            # has its own API field and must not make a Task look as though a
            # Reason Worker is running.
            legacy_reason = reason
            legacy_reason_present = reason_present
            decorated.append(
                task.model_copy(
                    update={
                        "fact_count": counts.get("facts", {}).get(task.id, 0),
                        "intent_count": counts.get("intents", {}).get(task.id, 0),
                        "working_intent_count": counts.get("working_intents", {}).get(task.id, 0),
                        "hint_count": counts.get("hints", {}).get(task.id, 0),
                        "todo_count": counts.get("todos", {}).get(task.id, 0),
                        "vulnerability_count": counts.get("vulnerabilities", {}).get(task.id, 0),
                        "artifact_count": counts.get("artifacts", {}).get(task.id, 0),
                        "blocked_work_count": sum(
                            counts.get(metric, {}).get(task.id, 0)
                            for metric in (
                                "blocked_intents",
                                "blocked_reason_runs",
                                "blocked_plan_runs",
                                "blocked_reviews",
                            )
                        ),
                        "reason_worker": legacy_reason["worker"] if legacy_reason_present else None,
                        "reason_heartbeat_at": legacy_reason["last_heartbeat_at"] if legacy_reason_present else None,
                        "reason_activity_kind": legacy_reason["activity_kind"] if legacy_reason_present else "",
                        "reason_activity_summary": (
                            legacy_reason["activity_summary"]
                            or {
                                "pending": "等待可用的 Reason Worker 领取",
                                "claimed": "正在启动 Reason 环境",
                                "preparing_context": "正在准备 Reason Context 与能力快照",
                                "analyzing": "正在分析任务上下文并规划下一批 Intent",
                            }.get(
                                legacy_reason["activity_kind"] or legacy_reason["status"],
                                "正在分析任务上下文并规划下一批 Intent",
                            )
                        )
                        if legacy_reason_present
                        else "",
                        "reason_activity_tool": legacy_reason["activity_tool"] if legacy_reason_present else "",
                        "reason_activity_started_at": (
                            legacy_reason["activity_started_at"] or legacy_reason["claimed_at"]
                            if legacy_reason_present
                            else None
                        ),
                        "reason_activity_updated_at": (
                            legacy_reason["activity_updated_at"] or legacy_reason["last_heartbeat_at"]
                            if legacy_reason_present
                            else None
                        ),
                        "active_reason_run": (
                            ActiveReasonRun(
                                id=reason["id"],
                                status=reason["status"],
                                trigger=reason["trigger"],
                                trigger_payload=json.loads(reason["trigger_payload_json"] or "{}"),
                                worker=reason["worker"],
                                lease_id=reason["lease_id"],
                                settings_version=reason["settings_version"],
                                execute_timeout_seconds=reason["execute_timeout_seconds"],
                                conclude_timeout_seconds=reason["conclude_timeout_seconds"],
                                last_heartbeat_at=reason["last_heartbeat_at"],
                                activity_kind=reason["activity_kind"] or "",
                                activity_summary=reason["activity_summary"] or "",
                                activity_tool=reason["activity_tool"] or "",
                                activity_started_at=reason["activity_started_at"],
                                activity_updated_at=reason["activity_updated_at"],
                                created_at=reason["created_at"],
                            )
                            if reason_present
                            else None
                        ),
                        "active_plan_run": (
                            ActivePlanRun(
                                id=plan["id"],
                                status=plan["status"],
                                trigger=plan["trigger"],
                                trigger_payload=_json_object(plan["trigger_payload_json"]),
                                worker=plan["worker"],
                                lease_id=plan["lease_id"],
                                settings_version=plan["settings_version"],
                                execute_timeout_seconds=plan["execute_timeout_seconds"],
                                conclude_timeout_seconds=plan["conclude_timeout_seconds"],
                                last_heartbeat_at=plan["last_heartbeat_at"],
                                activity_kind=plan["activity_kind"] or "",
                                activity_summary=plan["activity_summary"] or "",
                                activity_tool=plan["activity_tool"] or "",
                                activity_started_at=plan["activity_started_at"],
                                activity_updated_at=plan["activity_updated_at"],
                                created_at=plan["created_at"],
                            )
                            if plan_present
                            else None
                        ),
                        "queued_reason_runs": [
                            ActiveReasonRun(
                                id=row["id"],
                                status=row["status"],
                                trigger=row["trigger"],
                                trigger_payload=json.loads(row["trigger_payload_json"] or "{}"),
                                worker=row["worker"],
                                lease_id=row["lease_id"],
                                settings_version=row["settings_version"],
                                execute_timeout_seconds=row["execute_timeout_seconds"],
                                conclude_timeout_seconds=row["conclude_timeout_seconds"],
                                last_heartbeat_at=row["last_heartbeat_at"],
                                activity_kind=row["activity_kind"] or "",
                                activity_summary=row["activity_summary"] or "",
                                activity_tool=row["activity_tool"] or "",
                                activity_started_at=row["activity_started_at"],
                                activity_updated_at=row["activity_updated_at"],
                                created_at=row["created_at"],
                            )
                            for row in task_queued
                        ],
                        "queued_plan_runs": [
                            ActivePlanRun(
                                id=row["id"],
                                status=row["status"],
                                trigger=row["trigger"],
                                trigger_payload=_json_object(row["trigger_payload_json"]),
                                worker=row["worker"],
                                lease_id=row["lease_id"],
                                settings_version=row["settings_version"],
                                execute_timeout_seconds=row["execute_timeout_seconds"],
                                conclude_timeout_seconds=row["conclude_timeout_seconds"],
                                last_heartbeat_at=row["last_heartbeat_at"],
                                activity_kind=row["activity_kind"] or "",
                                activity_summary=row["activity_summary"] or "",
                                activity_tool=row["activity_tool"] or "",
                                activity_started_at=row["activity_started_at"],
                                activity_updated_at=row["activity_updated_at"],
                                created_at=row["created_at"],
                            )
                            for row in task_plan_queued
                        ],
                        "plan_run_count": plan_counts.get(task.id, 0),
                        "last_plan_decision": latest_plan_decisions.get(task.id),
                    }
                )
            )
        return decorated
