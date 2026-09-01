from __future__ import annotations

import json

from sqlalchemy import Connection, select, update

from axiom.modules.reason.schemas import ReasonRun, ReasonWorkspace, WorkspaceDraft
from axiom.modules.reason.tables import reason_run_sources, reason_runs, reason_workspaces
from axiom.shared.ids import allocate_id


ACTIVE_REASON_STATUSES = ("pending", "claimed", "running", "concluding")
LEASED_REASON_STATUSES = ("claimed", "running", "concluding")


class ReasonRepository:
    def create_run(
        self,
        conn: Connection,
        *,
        task_id: str,
        goal_cycle_id: str | None,
        source_fact_ids: list[str],
        trigger: str,
        trigger_payload: dict,
        input_fingerprint: str,
        worker_pool: list[str],
        settings_version: int,
        execute_timeout_seconds: int,
        conclude_timeout_seconds: int,
        target_open_intents: int,
        max_new_intents: int,
        explore_execute_min_seconds: int,
        explore_execute_max_seconds: int,
        explore_conclude_min_seconds: int,
        explore_conclude_max_seconds: int,
        now: str,
    ) -> ReasonRun:
        run_id = allocate_id(
            conn,
            scope="global",
            kind="reason_run",
            prefix="rr",
            width=6,
        )
        values = {
            "id": run_id,
            "task_id": task_id,
            "goal_cycle_id": goal_cycle_id,
            "status": "pending",
            "trigger": trigger,
            "trigger_payload_json": json.dumps(trigger_payload, ensure_ascii=False),
            "input_fingerprint": input_fingerprint,
            "worker_pool_json": json.dumps(worker_pool, ensure_ascii=False),
            "worker": None,
            "settings_version": settings_version,
            "execute_timeout_seconds": execute_timeout_seconds,
            "conclude_timeout_seconds": conclude_timeout_seconds,
            "target_open_intents": target_open_intents,
            "max_new_intents": max_new_intents,
            "explore_execute_min_seconds": explore_execute_min_seconds,
            "explore_execute_max_seconds": explore_execute_max_seconds,
            "explore_conclude_min_seconds": explore_conclude_min_seconds,
            "explore_conclude_max_seconds": explore_conclude_max_seconds,
            "lease_id": None,
            "claimed_at": None,
            "last_heartbeat_at": None,
            "retry_count": 0,
            "infrastructure_retry_count": 0,
            "next_retry_at": None,
            "last_failure_kind": "",
            "failure_class": "",
            "activity_kind": "pending",
            "activity_summary": "Reason 已就绪，等待空闲 Worker 领取",
            "activity_tool": "",
            "activity_started_at": None,
            "activity_updated_at": now,
            "created_at": now,
            "updated_at": now,
            "concluded_at": None,
        }
        conn.execute(reason_runs.insert().values(**values))
        source_fact_ids = list(dict.fromkeys(source_fact_ids))
        if source_fact_ids:
            conn.execute(
                reason_run_sources.insert(),
                [
                    {"task_id": task_id, "reason_run_id": run_id, "fact_id": fact_id}
                    for fact_id in source_fact_ids
                ],
            )
        return self._run(values, source_fact_ids)

    def get_run(self, conn: Connection, task_id: str, run_id: str) -> ReasonRun | None:
        row = conn.execute(
            select(reason_runs).where(
                reason_runs.c.task_id == task_id,
                reason_runs.c.id == run_id,
            )
        ).mappings().one_or_none()
        return self._run(row, self.source_ids(conn, task_id, run_id)) if row is not None else None

    def get_run_by_id(self, conn: Connection, run_id: str) -> ReasonRun | None:
        row = conn.execute(
            select(reason_runs).where(reason_runs.c.id == run_id)
        ).mappings().one_or_none()
        return (
            self._run(row, self.source_ids(conn, row["task_id"], run_id))
            if row is not None
            else None
        )

    @staticmethod
    def source_ids(conn: Connection, task_id: str, run_id: str) -> list[str]:
        return list(
            conn.execute(
                select(reason_run_sources.c.fact_id)
                .where(
                    reason_run_sources.c.task_id == task_id,
                    reason_run_sources.c.reason_run_id == run_id,
                )
                .order_by(reason_run_sources.c.fact_id)
            ).scalars()
        )

    def active_for_task(self, conn: Connection, task_id: str) -> ReasonRun | None:
        row = conn.execute(
            select(reason_runs)
            .where(
                reason_runs.c.task_id == task_id,
                reason_runs.c.status.in_(LEASED_REASON_STATUSES),
                # Legacy Plan rows remain readable through get_run(), but
                # must never participate in the Reason scheduler.
                reason_runs.c.trigger != "plan",
            )
            .order_by(reason_runs.c.created_at, reason_runs.c.id)
            .limit(1)
        ).mappings().one_or_none()
        return (
            self._run(row, self.source_ids(conn, task_id, row["id"]))
            if row is not None
            else None
        )

    def in_flight_for_task(self, conn: Connection, task_id: str) -> ReasonRun | None:
        row = conn.execute(
            select(reason_runs)
            .where(
                reason_runs.c.task_id == task_id,
                reason_runs.c.status.in_(["claimed", "running", "concluding"]),
                reason_runs.c.trigger != "plan",
            )
            .order_by(reason_runs.c.created_at, reason_runs.c.id)
            .limit(1)
        ).mappings().one_or_none()
        return (
            self._run(row, self.source_ids(conn, task_id, row["id"]))
            if row is not None
            else None
        )

    def pending_runs_for_task(self, conn: Connection, task_id: str) -> list[ReasonRun]:
        rows = conn.execute(
            select(reason_runs)
            .where(
                reason_runs.c.task_id == task_id,
                reason_runs.c.status == "pending",
                reason_runs.c.trigger != "plan",
            )
            .order_by(reason_runs.c.created_at, reason_runs.c.id)
        ).mappings().all()
        return [self._run(row, self.source_ids(conn, task_id, row["id"])) for row in rows]


    @staticmethod
    def update_pending_snapshot(
        conn: Connection,
        *,
        task_id: str,
        run_id: str,
        source_fact_ids: list[str],
        trigger_payload: dict,
        input_fingerprint: str,
        now: str,
    ) -> bool:
        result = conn.execute(
            update(reason_runs)
            .where(
                reason_runs.c.task_id == task_id,
                reason_runs.c.id == run_id,
                reason_runs.c.status == "pending",
                reason_runs.c.trigger != "plan",
            )
            .values(
                trigger_payload_json=json.dumps(trigger_payload, ensure_ascii=False),
                input_fingerprint=input_fingerprint,
                updated_at=now,
            )
        )
        if not result.rowcount:
            return False
        conn.execute(
            reason_run_sources.delete().where(
                reason_run_sources.c.task_id == task_id,
                reason_run_sources.c.reason_run_id == run_id,
            )
        )
        unique_ids = list(dict.fromkeys(source_fact_ids))
        if unique_ids:
            conn.execute(
                reason_run_sources.insert(),
                [
                    {"task_id": task_id, "reason_run_id": run_id, "fact_id": fact_id}
                    for fact_id in unique_ids
                ],
            )
        return True

    @staticmethod
    def update_run_snapshot(
        conn: Connection,
        *,
        task_id: str,
        run_id: str,
        source_fact_ids: list[str],
        trigger_payload: dict,
        input_fingerprint: str,
        now: str,
    ) -> bool:
        """Refresh a claimed/running snapshot without changing lease state."""
        result = conn.execute(
            update(reason_runs)
            .where(
                reason_runs.c.task_id == task_id,
                reason_runs.c.id == run_id,
                reason_runs.c.status.in_(["claimed", "running", "concluding"]),
                reason_runs.c.trigger != "plan",
            )
            .values(
                trigger_payload_json=json.dumps(trigger_payload, ensure_ascii=False),
                input_fingerprint=input_fingerprint,
                updated_at=now,
            )
        )
        if not result.rowcount:
            return False
        conn.execute(
            reason_run_sources.delete().where(
                reason_run_sources.c.task_id == task_id,
                reason_run_sources.c.reason_run_id == run_id,
            )
        )
        unique_ids = list(dict.fromkeys(source_fact_ids))
        if unique_ids:
            conn.execute(
                reason_run_sources.insert(),
                [
                    {"task_id": task_id, "reason_run_id": run_id, "fact_id": fact_id}
                    for fact_id in unique_ids
                ],
            )
        return True

    @staticmethod
    def update_trigger_payload(
        conn: Connection,
        *,
        task_id: str,
        run_id: str,
        trigger_payload: dict,
        now: str,
    ) -> bool:
        result = conn.execute(
            update(reason_runs)
            .where(
                reason_runs.c.task_id == task_id,
                reason_runs.c.id == run_id,
                reason_runs.c.status.in_(["running", "concluding"]),
                reason_runs.c.trigger != "plan",
            )
            .values(
                trigger_payload_json=json.dumps(trigger_payload, ensure_ascii=False),
                updated_at=now,
            )
        )
        return bool(result.rowcount)

    @staticmethod
    def latest_completed_row(conn: Connection, task_id: str):
        return conn.execute(
            select(reason_runs)
            .where(
                reason_runs.c.task_id == task_id,
                reason_runs.c.status == "completed",
                reason_runs.c.trigger != "plan",
            )
            .order_by(
                reason_runs.c.concluded_at.desc().nulls_last(),
                reason_runs.c.id.desc(),
            )
            .limit(1)
        ).mappings().one_or_none()

    @staticmethod
    def latest_finalized_row(conn: Connection, task_id: str):
        """Return the latest terminal regular ReasonRun, including failures.

        Plan handoff is a one-shot edge.  Looking only at completed ReasonRuns
        makes a terminal failed handoff appear to have never been consumed,
        so every reconciliation tick can create another fresh handoff and
        bypass the Reason retry budget.
        """

        return conn.execute(
            select(reason_runs)
            .where(
                reason_runs.c.task_id == task_id,
                reason_runs.c.status.in_(("completed", "failed", "cancelled")),
                reason_runs.c.trigger != "plan",
            )
            .order_by(
                reason_runs.c.concluded_at.desc().nulls_last(),
                reason_runs.c.updated_at.desc(),
                reason_runs.c.id.desc(),
            )
            .limit(1)
        ).mappings().one_or_none()

    @staticmethod
    def latest_deduplication_row(conn: Connection, task_id: str):
        """Return the latest finalized planning input that must not be replayed."""

        return conn.execute(
            select(reason_runs)
            .where(
                reason_runs.c.task_id == task_id,
                reason_runs.c.status.in_(["completed", "failed"]),
                reason_runs.c.trigger != "plan",
            )
            .order_by(
                reason_runs.c.concluded_at.desc().nulls_last(),
                reason_runs.c.id.desc(),
            )
            .limit(1)
        ).mappings().one_or_none()

    @staticmethod
    def claim(
        conn: Connection,
        *,
        task_id: str,
        run_id: str,
        worker_name: str,
        lease_id: str,
        settings_version: int,
        execute_timeout_seconds: int,
        conclude_timeout_seconds: int,
        target_open_intents: int,
        max_new_intents: int,
        explore_execute_min_seconds: int,
        explore_execute_max_seconds: int,
        explore_conclude_min_seconds: int,
        explore_conclude_max_seconds: int,
        now: str,
    ) -> bool:
        result = conn.execute(
            update(reason_runs)
            .where(
                reason_runs.c.task_id == task_id,
                reason_runs.c.id == run_id,
                reason_runs.c.status == "pending",
                reason_runs.c.trigger != "plan",
            )
            .values(
                status="claimed",
                worker=worker_name,
                lease_id=lease_id,
                claimed_at=now,
                last_heartbeat_at=now,
                settings_version=settings_version,
                execute_timeout_seconds=execute_timeout_seconds,
                conclude_timeout_seconds=conclude_timeout_seconds,
                target_open_intents=target_open_intents,
                max_new_intents=max_new_intents,
                explore_execute_min_seconds=explore_execute_min_seconds,
                explore_execute_max_seconds=explore_execute_max_seconds,
                explore_conclude_min_seconds=explore_conclude_min_seconds,
                explore_conclude_max_seconds=explore_conclude_max_seconds,
                next_retry_at=None,
                last_failure_kind="",
                failure_class="",
                activity_kind="claimed",
                activity_summary=f"已由 {worker_name} 领取，正在启动 Reason 环境",
                activity_tool="",
                activity_started_at=now,
                activity_updated_at=now,
                updated_at=now,
            )
        )
        return bool(result.rowcount)

    @staticmethod
    def start(
        conn: Connection,
        *,
        task_id: str,
        run_id: str,
        lease_id: str,
        now: str,
    ) -> bool:
        result = conn.execute(
            update(reason_runs)
            .where(
                reason_runs.c.task_id == task_id,
                reason_runs.c.id == run_id,
                reason_runs.c.status == "claimed",
                reason_runs.c.lease_id == lease_id,
                reason_runs.c.trigger != "plan",
            )
            .values(
                status="running",
                last_failure_kind="",
                failure_class="",
                last_heartbeat_at=now,
                activity_kind="preparing_context",
                activity_summary="正在准备 Reason Context 与能力快照",
                activity_updated_at=now,
                updated_at=now,
            )
        )
        return bool(result.rowcount)

    @staticmethod
    def heartbeat(
        conn: Connection,
        *,
        task_id: str,
        run_id: str,
        lease_id: str,
        now: str,
    ) -> bool:
        result = conn.execute(
            update(reason_runs)
            .where(
                reason_runs.c.task_id == task_id,
                reason_runs.c.id == run_id,
                reason_runs.c.lease_id == lease_id,
                reason_runs.c.status.in_(LEASED_REASON_STATUSES),
                reason_runs.c.trigger != "plan",
            )
            .values(last_heartbeat_at=now, activity_updated_at=now, updated_at=now)
        )
        return bool(result.rowcount)

    @staticmethod
    def update_activity(
        conn: Connection,
        *,
        task_id: str,
        run_id: str,
        lease_id: str,
        activity_kind: str,
        activity_summary: str,
        activity_tool: str,
        now: str,
    ) -> bool:
        result = conn.execute(
            update(reason_runs)
            .where(
                reason_runs.c.task_id == task_id,
                reason_runs.c.id == run_id,
                reason_runs.c.lease_id == lease_id,
                reason_runs.c.status.in_(LEASED_REASON_STATUSES),
                reason_runs.c.trigger != "plan",
            )
            .values(
                activity_kind=activity_kind,
                activity_summary=activity_summary,
                activity_tool=activity_tool,
                activity_updated_at=now,
                updated_at=now,
            )
        )
        return bool(result.rowcount)

    @staticmethod
    def begin_conclude(
        conn: Connection,
        *,
        task_id: str,
        run_id: str,
        lease_id: str,
        now: str,
    ) -> bool:
        result = conn.execute(
            update(reason_runs)
            .where(
                reason_runs.c.task_id == task_id,
                reason_runs.c.id == run_id,
                reason_runs.c.lease_id == lease_id,
                reason_runs.c.status == "running",
                reason_runs.c.trigger != "plan",
            )
            .values(
                status="concluding",
                last_heartbeat_at=now,
                activity_kind="concluding",
                activity_summary="正在整理 Reason Workspace 并完成收口校验",
                activity_tool="",
                activity_updated_at=now,
                updated_at=now,
            )
        )
        return bool(result.rowcount)

    @staticmethod
    def release(
        conn: Connection,
        *,
        task_id: str,
        run_id: str,
        lease_id: str,
        failure_kind: str,
        failure_class: str,
        next_retry_at: str | None,
        terminal: bool,
        now: str,
    ) -> bool:
        result = conn.execute(
            update(reason_runs)
            .where(
                reason_runs.c.task_id == task_id,
                reason_runs.c.id == run_id,
                reason_runs.c.lease_id == lease_id,
                reason_runs.c.status.in_(LEASED_REASON_STATUSES),
                reason_runs.c.trigger != "plan",
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
                retry_count=reason_runs.c.retry_count + (1 if failure_class == "execution" else 0),
                infrastructure_retry_count=reason_runs.c.infrastructure_retry_count
                + (1 if failure_class == "transient" else 0),
                activity_kind=(
                    "failed"
                    if terminal
                    else "retry_blocked"
                    if failure_class == "transient" and next_retry_at is None
                    else "retry_wait"
                ),
                activity_summary=(
                    f"Reason 执行失败：{failure_kind}"
                    if terminal
                    else (
                        f"Reason 基础设施重试已达上限，等待人工或系统恢复：{failure_kind}"
                        if failure_class == "transient" and next_retry_at is None
                        else f"Reason 已释放，等待重试：{failure_kind}"
                    )
                ),
                activity_tool="",
                activity_updated_at=now,
                updated_at=now,
                concluded_at=now if terminal else None,
            )
        )
        return bool(result.rowcount)

    @staticmethod
    def complete(
        conn: Connection,
        *,
        task_id: str,
        run_id: str,
        lease_id: str,
        now: str,
    ) -> bool:
        result = conn.execute(
            update(reason_runs)
            .where(
                reason_runs.c.task_id == task_id,
                reason_runs.c.id == run_id,
                reason_runs.c.lease_id == lease_id,
                reason_runs.c.status.in_(["running", "concluding"]),
                reason_runs.c.trigger != "plan",
            )
            .values(
                status="completed",
                last_heartbeat_at=now,
                next_retry_at=None,
                last_failure_kind="",
                failure_class="",
                activity_kind="completed",
                activity_summary="Reason 已完成规划并提交",
                activity_tool="",
                activity_updated_at=now,
                updated_at=now,
                concluded_at=now,
            )
        )
        return bool(result.rowcount)

    @staticmethod
    def cancel_open_for_task(
        conn: Connection,
        task_id: str,
        *,
        now: str,
        failure_kind: str = "task_inactive",
    ) -> int:
        result = conn.execute(
            update(reason_runs)
            .where(
                reason_runs.c.task_id == task_id,
                reason_runs.c.status.in_(ACTIVE_REASON_STATUSES),
                reason_runs.c.trigger != "plan",
            )
            .values(
                status="cancelled",
                worker=None,
                lease_id=None,
                claimed_at=None,
                last_heartbeat_at=None,
                next_retry_at=None,
                last_failure_kind=failure_kind,
                failure_class="",
                activity_kind="cancelled",
                activity_summary=(
                    "任务运行时间已到，ReasonRun 已取消"
                    if failure_kind == "run_deadline_reached"
                    else "任务已停止，ReasonRun 已取消"
                ),
                activity_tool="",
                activity_updated_at=now,
                updated_at=now,
                concluded_at=now,
            )
        )
        return int(result.rowcount or 0)

    @staticmethod
    def cancel_pending_plan_runs(
        conn: Connection,
        task_id: str,
        *,
        now: str,
        failure_kind: str = "incremental_work_arrived",
    ) -> int:
        result = conn.execute(
            update(reason_runs)
            .where(
                reason_runs.c.task_id == task_id,
                reason_runs.c.trigger == "plan",
                reason_runs.c.status == "pending",
            )
            .values(
                status="cancelled",
                worker=None,
                lease_id=None,
                claimed_at=None,
                last_heartbeat_at=None,
                next_retry_at=None,
                last_failure_kind=failure_kind,
                failure_class="",
                activity_kind="cancelled",
                activity_summary="增量 Reason 输入到达，Plan 已取消并重新排队",
                activity_tool="",
                activity_updated_at=now,
                updated_at=now,
                concluded_at=now,
            )
        )
        return int(result.rowcount or 0)


    @staticmethod
    def stale_active(conn: Connection, *, cutoff: str):
        return conn.execute(
            select(reason_runs).where(
                reason_runs.c.status.in_(LEASED_REASON_STATUSES),
                reason_runs.c.lease_id.is_not(None),
                reason_runs.c.last_heartbeat_at.is_not(None),
                reason_runs.c.last_heartbeat_at <= cutoff,
                reason_runs.c.trigger != "plan",
            )
        ).mappings().all()

    def open(
        self,
        conn: Connection,
        *,
        task_id: str,
        reason_run_id: str,
        lease_id: str,
        worker: str,
        context_revision: int,
        now: str,
    ) -> ReasonWorkspace:
        workspace_id = allocate_id(
            conn,
            scope="global",
            kind="reason_workspace",
            prefix="rw_",
            width=6,
        )
        values = {
            "id": workspace_id,
            "task_id": task_id,
            "intent_id": None,
            "reason_run_id": reason_run_id,
            "lease_id": lease_id,
            "status": "open",
            "context_revision": context_revision,
            "draft_json": WorkspaceDraft().model_dump_json(),
            "worker": worker,
            "failure_reason": "",
            "result_json": "{}",
            "created_at": now,
            "updated_at": now,
            "committed_at": None,
        }
        conn.execute(reason_workspaces.insert().values(**values))
        return self._workspace(values)

    def get(self, conn: Connection, workspace_id: str) -> ReasonWorkspace | None:
        row = conn.execute(
            select(reason_workspaces).where(reason_workspaces.c.id == workspace_id)
        ).mappings().one_or_none()
        return self._workspace(row) if row is not None else None

    def for_attempt(
        self,
        conn: Connection,
        task_id: str,
        reason_run_id: str,
        lease_id: str,
    ) -> ReasonWorkspace | None:
        row = conn.execute(
            select(reason_workspaces).where(
                reason_workspaces.c.task_id == task_id,
                reason_workspaces.c.reason_run_id == reason_run_id,
                reason_workspaces.c.lease_id == lease_id,
            )
        ).mappings().one_or_none()
        return self._workspace(row) if row is not None else None

    def save_draft(
        self,
        conn: Connection,
        workspace_id: str,
        draft: WorkspaceDraft,
        *,
        now: str,
    ) -> ReasonWorkspace:
        result = conn.execute(
            update(reason_workspaces)
            .where(reason_workspaces.c.id == workspace_id, reason_workspaces.c.status == "open")
            .values(draft_json=draft.model_dump_json(), updated_at=now)
        )
        if not result.rowcount:
            raise ValueError("Reason Workspace is not open")
        workspace = self.get(conn, workspace_id)
        assert workspace is not None
        return workspace

    def mark_committed(
        self,
        conn: Connection,
        workspace_id: str,
        *,
        created_todo_ids: list[str],
        created_intent_ids: list[str],
        now: str,
    ) -> ReasonWorkspace:
        result_json = json.dumps(
            {
                "created_todo_ids": created_todo_ids,
                "created_intent_ids": created_intent_ids,
            },
            ensure_ascii=False,
        )
        result = conn.execute(
            update(reason_workspaces)
            .where(reason_workspaces.c.id == workspace_id, reason_workspaces.c.status == "open")
            .values(
                status="committed",
                result_json=result_json,
                updated_at=now,
                committed_at=now,
            )
        )
        if not result.rowcount:
            raise ValueError("Reason Workspace is not open")
        workspace = self.get(conn, workspace_id)
        assert workspace is not None
        return workspace

    def commit_result(
        self,
        conn: Connection,
        workspace: ReasonWorkspace,
    ) -> tuple[list[str], list[str]]:
        row = conn.execute(
            select(reason_workspaces.c.result_json).where(reason_workspaces.c.id == workspace.id)
        ).scalar_one()
        payload = json.loads(row or "{}")
        return (
            list(payload.get("created_todo_ids", [])),
            list(payload.get("created_intent_ids", [])),
        )

    def abandon(
        self,
        conn: Connection,
        workspace_id: str,
        *,
        failure_reason: str,
        now: str,
    ) -> ReasonWorkspace:
        result = conn.execute(
            update(reason_workspaces)
            .where(reason_workspaces.c.id == workspace_id, reason_workspaces.c.status == "open")
            .values(status="abandoned", failure_reason=failure_reason, updated_at=now)
        )
        if not result.rowcount:
            raise ValueError("Reason Workspace is not open")
        workspace = self.get(conn, workspace_id)
        assert workspace is not None
        return workspace

    @staticmethod
    def abandon_open(
        conn: Connection,
        *,
        task_id: str,
        failure_reason: str,
        now: str,
        reason_run_id: str | None = None,
        lease_id: str | None = None,
        exclude_workspace_id: str | None = None,
    ) -> int:
        conditions = [
            reason_workspaces.c.task_id == task_id,
            reason_workspaces.c.status == "open",
        ]
        if reason_run_id is not None:
            conditions.append(reason_workspaces.c.reason_run_id == reason_run_id)
        if lease_id is not None:
            conditions.append(reason_workspaces.c.lease_id == lease_id)
        if exclude_workspace_id is not None:
            conditions.append(reason_workspaces.c.id != exclude_workspace_id)
        result = conn.execute(
            update(reason_workspaces)
            .where(*conditions)
            .values(
                status="abandoned",
                failure_reason=failure_reason,
                updated_at=now,
            )
        )
        return int(result.rowcount or 0)

    @staticmethod
    def _workspace(row) -> ReasonWorkspace:
        return ReasonWorkspace(
            id=row["id"],
            task_id=row["task_id"],
            goal_cycle_id=row.get("goal_cycle_id"),
            intent_id=row.get("intent_id"),
            reason_run_id=row.get("reason_run_id"),
            lease_id=row["lease_id"],
            status=row["status"],
            context_revision=row["context_revision"],
            draft=WorkspaceDraft.model_validate_json(row["draft_json"] or "{}"),
            worker=row["worker"],
            failure_reason=row["failure_reason"] or "",
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            committed_at=row["committed_at"],
        )

    @staticmethod
    def _run(row, source_fact_ids: list[str]) -> ReasonRun:
        return ReasonRun(
            id=row["id"],
            task_id=row["task_id"],
            goal_cycle_id=row.get("goal_cycle_id"),
            status=row["status"],
            trigger=row["trigger"],
            legacy_plan=row["trigger"] == "plan",
            trigger_payload=json.loads(row["trigger_payload_json"] or "{}"),
            input_fingerprint=row["input_fingerprint"],
            source_fact_ids=source_fact_ids,
            worker_pool=json.loads(row["worker_pool_json"] or "[]"),
            worker=row["worker"],
            settings_version=row["settings_version"],
            execute_timeout_seconds=row["execute_timeout_seconds"],
            conclude_timeout_seconds=row["conclude_timeout_seconds"],
            target_open_intents=row["target_open_intents"],
            max_new_intents=row["max_new_intents"],
            explore_execute_min_seconds=row["explore_execute_min_seconds"],
            explore_execute_max_seconds=row["explore_execute_max_seconds"],
            explore_conclude_min_seconds=row["explore_conclude_min_seconds"],
            explore_conclude_max_seconds=row["explore_conclude_max_seconds"],
            lease_id=row["lease_id"],
            claimed_at=row["claimed_at"],
            last_heartbeat_at=row["last_heartbeat_at"],
            retry_count=row["retry_count"],
            infrastructure_retry_count=row["infrastructure_retry_count"],
            next_retry_at=row["next_retry_at"],
            last_failure_kind=row["last_failure_kind"] or "",
            failure_class=row["failure_class"] or "",
            activity_kind=row["activity_kind"] or "",
            activity_summary=row["activity_summary"] or "",
            activity_tool=row["activity_tool"] or "",
            activity_started_at=row["activity_started_at"],
            activity_updated_at=row["activity_updated_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            concluded_at=row["concluded_at"],
        )
