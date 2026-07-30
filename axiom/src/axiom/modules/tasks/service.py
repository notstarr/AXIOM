from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import Engine, func, literal, select, union_all, update

from axiom.modules.audit.schemas import RecordAuditEvent
from axiom.modules.audit.service import AuditService
from axiom.modules.artifacts.tables import artifacts
from axiom.modules.graph.repository import GraphRepository
from axiom.modules.graph.tables import facts, intents
from axiom.modules.goals.repository import GoalRepository
from axiom.modules.goals.tables import task_goal_cycles
from axiom.modules.projects.repository import ProjectRepository
from axiom.modules.reason.repository import ReasonRepository
from axiom.modules.reason.service import ReasonService
from axiom.modules.reason.tables import reason_runs
from axiom.modules.tasks.origin import parse_origin
from axiom.modules.tasks.repository import TaskRepository
from axiom.modules.tasks.schemas import (
    ActiveReasonRun,
    ContinueTask,
    ContinueTaskResult,
    CreateTask,
    CreateTaskHint,
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
from axiom.shared.clock import format_utc, parse_utc, utcnow
from axiom.shared.database import immediate_transaction
from axiom.shared.errors import ConflictError, NotFoundError


class TaskService:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.projects = ProjectRepository()
        self.tasks = TaskRepository()
        self.graph = GraphRepository()
        self.goals = GoalRepository()
        self.reason = ReasonRepository()
        self.reason_service = ReasonService(engine)
        self.audit = AuditService(engine)

    def create(self, command: CreateTask) -> Task:
        origin = parse_origin(command.origin)
        scope = command.scope or TaskScope(enabled=True, targets=[origin.host])
        if scope.enabled and not scope.targets:
            scope = scope.model_copy(update={"targets": [origin.host]})
        now = utcnow()
        deadline_at = self._deadline_at(now, command.run_duration_seconds)
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
                scope=scope,
                worker_pool=command.worker_pool,
                guard_enabled=command.guard_enabled,
                external_report_enabled=command.external_report_enabled,
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
            if command.goal is not None and task.status == "completed":
                raise ConflictError("已完成任务必须通过继续任务创建新的 Goal")
            if command.scope is not None and command.scope.enabled and not command.scope.targets:
                command = command.model_copy(
                    update={"scope": command.scope.model_copy(update={"targets": [task.origin.host]})}
                )
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
                    .where(reason_runs.c.task_id == task_id, reason_runs.c.status == "pending")
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
        with immediate_transaction(self.engine) as conn:
            task = self.tasks.get(conn, task_id)
            if task is None:
                raise NotFoundError(f"Task not found: {task_id}")
            if task.legacy_readonly:
                raise ConflictError("历史只读任务不能继续运行")
            if task.status != "completed":
                raise ConflictError("只有已完成任务可以继续运行")
            if self.reason.active_for_task(conn, task_id) is not None:
                raise ConflictError("任务已有活动 ReasonRun")

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

    def reconcile_run_deadlines(self) -> RunDeadlineReconcileReport:
        checked_at = utcnow()
        with self.engine.connect() as conn:
            candidates = self.tasks.expired_running_ids(conn, now=checked_at)
        paused_task_ids: list[str] = []
        for task_id in candidates:
            with immediate_transaction(self.engine) as conn:
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
        with self.engine.begin() as conn:
            task = self.tasks.get(conn, task_id)
            if task is None:
                raise NotFoundError(f"Task not found: {task_id}")
            if task.status == "running":
                raise ConflictError("Stop a running Task before deleting it")
            self.tasks.delete(conn, task_id)

    def hints(self, task_id: str) -> list[TaskHint]:
        with self.engine.connect() as conn:
            if self.tasks.get(conn, task_id) is None:
                raise NotFoundError(f"Task not found: {task_id}")
            return self.tasks.hints(conn, task_id)

    def add_hint(self, task_id: str, command: CreateTaskHint) -> TaskHint:
        with self.engine.begin() as conn:
            task = self.tasks.get(conn, task_id)
            if task is None:
                raise NotFoundError(f"Task not found: {task_id}")
            if task.legacy_readonly:
                raise ConflictError("Legacy Task is read-only")
            return self.tasks.create_hint(conn, task_id=task_id, command=command, now=utcnow())

    def update_hint(self, task_id: str, hint_id: str, command: UpdateTaskHint) -> TaskHint:
        with self.engine.begin() as conn:
            task = self.tasks.get(conn, task_id)
            if task is None:
                raise NotFoundError(f"Task not found: {task_id}")
            if task.legacy_readonly:
                raise ConflictError("Legacy Task is read-only")
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
                )
            ).mappings()
            counts: dict[str, dict[str, int]] = {}
            for row in count_rows:
                counts.setdefault(str(row["metric"]), {})[str(row["task_id"])] = int(
                    row["count"] or 0
                )
            reason_rows = conn.execute(
                select(reason_runs)
                .where(
                    reason_runs.c.task_id.in_(task_ids),
                    reason_runs.c.status.in_(["pending", "claimed", "running", "concluding"]),
                )
                .order_by(
                    reason_runs.c.task_id,
                    reason_runs.c.created_at.desc(),
                    reason_runs.c.id.desc(),
                )
            ).mappings()
            reasons: dict[str, object] = {}
            for row in reason_rows:
                reasons.setdefault(str(row["task_id"]), row)

        decorated: list[Task] = []
        for task in tasks_to_decorate:
            reason = reasons.get(task.id)
            reason_present = reason is not None
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
                        "reason_worker": reason["worker"] if reason_present else None,
                        "reason_heartbeat_at": reason["last_heartbeat_at"] if reason_present else None,
                        "reason_activity_kind": reason["activity_kind"] if reason_present else "",
                        "reason_activity_summary": (
                            reason["activity_summary"]
                            or "正在分析任务上下文并规划下一批 Intent"
                        )
                        if reason_present
                        else "",
                        "reason_activity_tool": reason["activity_tool"] if reason_present else "",
                        "reason_activity_started_at": (
                            reason["activity_started_at"] or reason["claimed_at"]
                            if reason_present
                            else None
                        ),
                        "reason_activity_updated_at": (
                            reason["activity_updated_at"] or reason["last_heartbeat_at"]
                            if reason_present
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
                    }
                )
            )
        return decorated
