from __future__ import annotations

import json

from sqlalchemy import Connection, select, update

from axiom.modules.plan.schemas import PlanRun, PlanWorkspace, PlanWorkspaceDraft
from axiom.modules.plan.tables import plan_run_sources, plan_runs, plan_workspaces
from axiom.shared.ids import allocate_id


ACTIVE_PLAN_STATUSES = ("pending", "claimed", "running", "concluding")
LEASED_PLAN_STATUSES = ("claimed", "running", "concluding")


def _json(value, default):
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value or "")
        except (TypeError, ValueError):
            return default
    return value


class PlanRepository:
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
        now: str,
    ) -> PlanRun:
        run_id = allocate_id(conn, scope="global", kind="plan_run", prefix="pr", width=6)
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
            "lease_id": None,
            "claimed_at": None,
            "last_heartbeat_at": None,
            "retry_count": 0,
            "infrastructure_retry_count": 0,
            "next_retry_at": None,
            "last_failure_kind": "",
            "failure_class": "",
            "activity_kind": "pending",
            "activity_summary": "Plan 已就绪，等待 Plan Worker 领取",
            "activity_tool": "",
            "activity_started_at": None,
            "activity_updated_at": now,
            "created_at": now,
            "updated_at": now,
            "concluded_at": None,
            "decision_json": "{}",
        }
        conn.execute(plan_runs.insert().values(**values))
        unique_ids = list(dict.fromkeys(source_fact_ids))
        if unique_ids:
            conn.execute(
                plan_run_sources.insert(),
                [
                    {"task_id": task_id, "plan_run_id": run_id, "fact_id": fact_id}
                    for fact_id in unique_ids
                ],
            )
        return self._run(values, unique_ids)

    def get_run(self, conn: Connection, task_id: str, run_id: str) -> PlanRun | None:
        row = conn.execute(
            select(plan_runs).where(plan_runs.c.task_id == task_id, plan_runs.c.id == run_id)
        ).mappings().one_or_none()
        return self._run(row, self.source_ids(conn, task_id, run_id)) if row else None

    def get_run_by_id(self, conn: Connection, run_id: str) -> PlanRun | None:
        row = conn.execute(select(plan_runs).where(plan_runs.c.id == run_id)).mappings().one_or_none()
        return self._run(row, self.source_ids(conn, row["task_id"], run_id)) if row else None

    @staticmethod
    def source_ids(conn: Connection, task_id: str, run_id: str) -> list[str]:
        return list(
            conn.execute(
                select(plan_run_sources.c.fact_id)
                .where(plan_run_sources.c.task_id == task_id, plan_run_sources.c.plan_run_id == run_id)
                .order_by(plan_run_sources.c.fact_id)
            ).scalars()
        )

    def active_for_task(self, conn: Connection, task_id: str) -> PlanRun | None:
        row = conn.execute(
            select(plan_runs)
            .where(plan_runs.c.task_id == task_id, plan_runs.c.status.in_(LEASED_PLAN_STATUSES))
            .order_by(plan_runs.c.created_at, plan_runs.c.id)
            .limit(1)
        ).mappings().one_or_none()
        return self._run(row, self.source_ids(conn, task_id, row["id"])) if row else None

    def in_flight_for_task(self, conn: Connection, task_id: str) -> PlanRun | None:
        return self.active_for_task(conn, task_id)

    def pending_runs_for_task(self, conn: Connection, task_id: str) -> list[PlanRun]:
        rows = conn.execute(
            select(plan_runs)
            .where(plan_runs.c.task_id == task_id, plan_runs.c.status == "pending")
            .order_by(plan_runs.c.created_at, plan_runs.c.id)
        ).mappings().all()
        return [self._run(row, self.source_ids(conn, task_id, row["id"])) for row in rows]

    @staticmethod
    def update_snapshot(
        conn: Connection,
        *,
        task_id: str,
        run_id: str,
        source_fact_ids: list[str],
        trigger_payload: dict,
        input_fingerprint: str,
        now: str,
        active: bool = False,
    ) -> bool:
        statuses = LEASED_PLAN_STATUSES if active else ("pending",)
        result = conn.execute(
            update(plan_runs)
            .where(
                plan_runs.c.task_id == task_id,
                plan_runs.c.id == run_id,
                plan_runs.c.status.in_(statuses),
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
            plan_run_sources.delete().where(
                plan_run_sources.c.task_id == task_id,
                plan_run_sources.c.plan_run_id == run_id,
            )
        )
        unique_ids = list(dict.fromkeys(source_fact_ids))
        if unique_ids:
            conn.execute(
                plan_run_sources.insert(),
                [
                    {"task_id": task_id, "plan_run_id": run_id, "fact_id": fact_id}
                    for fact_id in unique_ids
                ],
            )
        return True

    @staticmethod
    def update_pending_snapshot(conn: Connection, **kwargs) -> bool:
        return PlanRepository.update_snapshot(conn, active=False, **kwargs)

    @staticmethod
    def update_run_snapshot(conn: Connection, **kwargs) -> bool:
        return PlanRepository.update_snapshot(conn, active=True, **kwargs)

    @staticmethod
    def update_decision(conn: Connection, *, task_id: str, run_id: str, decision: dict, now: str) -> bool:
        result = conn.execute(
            update(plan_runs)
            .where(plan_runs.c.task_id == task_id, plan_runs.c.id == run_id)
            .values(decision_json=json.dumps(decision, ensure_ascii=False), updated_at=now)
        )
        return bool(result.rowcount)

    @staticmethod
    def claim(conn: Connection, *, task_id: str, run_id: str, worker_name: str, lease_id: str, settings_version: int, execute_timeout_seconds: int, conclude_timeout_seconds: int, now: str) -> bool:
        result = conn.execute(
            update(plan_runs)
            .where(plan_runs.c.task_id == task_id, plan_runs.c.id == run_id, plan_runs.c.status == "pending")
            .values(
                status="claimed", worker=worker_name, lease_id=lease_id, claimed_at=now,
                last_heartbeat_at=now, settings_version=settings_version,
                execute_timeout_seconds=execute_timeout_seconds,
                conclude_timeout_seconds=conclude_timeout_seconds,
                next_retry_at=None, last_failure_kind="", failure_class="",
                activity_kind="claimed", activity_summary=f"已由 {worker_name} 领取，正在启动 Plan 环境",
                activity_started_at=now, activity_updated_at=now, updated_at=now,
            )
        )
        return bool(result.rowcount)

    @staticmethod
    def start(conn: Connection, *, task_id: str, run_id: str, lease_id: str, now: str) -> bool:
        result = conn.execute(
            update(plan_runs)
            .where(plan_runs.c.task_id == task_id, plan_runs.c.id == run_id, plan_runs.c.lease_id == lease_id, plan_runs.c.status == "claimed")
            .values(status="running", last_heartbeat_at=now, activity_kind="preparing_context", activity_summary="正在准备 Plan 全景 Context 与能力快照", activity_updated_at=now, updated_at=now)
        )
        return bool(result.rowcount)

    @staticmethod
    def heartbeat(conn: Connection, *, task_id: str, run_id: str, lease_id: str, now: str) -> bool:
        result = conn.execute(
            update(plan_runs)
            .where(plan_runs.c.task_id == task_id, plan_runs.c.id == run_id, plan_runs.c.lease_id == lease_id, plan_runs.c.status.in_(LEASED_PLAN_STATUSES))
            .values(last_heartbeat_at=now, activity_updated_at=now, updated_at=now)
        )
        return bool(result.rowcount)

    @staticmethod
    def update_activity(conn: Connection, *, task_id: str, run_id: str, lease_id: str, activity_kind: str, activity_summary: str, activity_tool: str, now: str) -> bool:
        result = conn.execute(
            update(plan_runs)
            .where(plan_runs.c.task_id == task_id, plan_runs.c.id == run_id, plan_runs.c.lease_id == lease_id, plan_runs.c.status.in_(LEASED_PLAN_STATUSES))
            .values(activity_kind=activity_kind, activity_summary=activity_summary, activity_tool=activity_tool, activity_updated_at=now, updated_at=now)
        )
        return bool(result.rowcount)

    @staticmethod
    def begin_conclude(conn: Connection, *, task_id: str, run_id: str, lease_id: str, now: str) -> bool:
        result = conn.execute(
            update(plan_runs)
            .where(plan_runs.c.task_id == task_id, plan_runs.c.id == run_id, plan_runs.c.lease_id == lease_id, plan_runs.c.status == "running")
            .values(status="concluding", last_heartbeat_at=now, activity_kind="concluding", activity_summary="正在整理 Plan Workspace 并完成收口校验", activity_updated_at=now, updated_at=now)
        )
        return bool(result.rowcount)

    @staticmethod
    def release(conn: Connection, *, task_id: str, run_id: str, lease_id: str, failure_kind: str, failure_class: str, next_retry_at: str | None, terminal: bool, now: str) -> bool:
        result = conn.execute(
            update(plan_runs)
            .where(plan_runs.c.task_id == task_id, plan_runs.c.id == run_id, plan_runs.c.lease_id == lease_id, plan_runs.c.status.in_(LEASED_PLAN_STATUSES))
            .values(
                status="failed" if terminal else "pending", worker=None, lease_id=None, claimed_at=None, last_heartbeat_at=None,
                next_retry_at=None if terminal else next_retry_at, last_failure_kind=failure_kind, failure_class=failure_class,
                retry_count=plan_runs.c.retry_count + (1 if failure_class == "execution" else 0),
                infrastructure_retry_count=plan_runs.c.infrastructure_retry_count + (1 if failure_class == "transient" else 0),
                activity_kind=("failed" if terminal else "retry_blocked" if failure_class == "transient" and next_retry_at is None else "retry_wait"), activity_summary=(f"Plan 执行失败：{failure_kind}" if terminal else f"Plan 基础设施重试已达上限，等待人工或系统恢复：{failure_kind}" if failure_class == "transient" and next_retry_at is None else f"Plan 已释放，等待重试：{failure_kind}"), activity_tool="", activity_updated_at=now, updated_at=now, concluded_at=now if terminal else None,
            )
        )
        return bool(result.rowcount)

    @staticmethod
    def complete(conn: Connection, *, task_id: str, run_id: str, lease_id: str, now: str) -> bool:
        result = conn.execute(
            update(plan_runs)
            .where(plan_runs.c.task_id == task_id, plan_runs.c.id == run_id, plan_runs.c.lease_id == lease_id, plan_runs.c.status.in_(["running", "concluding"]))
            .values(status="completed", last_heartbeat_at=now, next_retry_at=None, last_failure_kind="", failure_class="", activity_kind="completed", activity_summary="Plan 已完成全景复盘并提交", activity_tool="", activity_updated_at=now, updated_at=now, concluded_at=now)
        )
        return bool(result.rowcount)

    @staticmethod
    def cancel_open_for_task(conn: Connection, task_id: str, *, now: str, failure_kind: str = "task_inactive") -> int:
        result = conn.execute(
            update(plan_runs)
            .where(plan_runs.c.task_id == task_id, plan_runs.c.status.in_(ACTIVE_PLAN_STATUSES))
            .values(status="cancelled", worker=None, lease_id=None, claimed_at=None, last_heartbeat_at=None, next_retry_at=None, last_failure_kind=failure_kind, failure_class="", activity_kind="cancelled", activity_summary="任务已停止，PlanRun 已取消", activity_tool="", activity_updated_at=now, updated_at=now, concluded_at=now)
        )
        return int(result.rowcount or 0)

    @staticmethod
    def cancel_pending_for_task(
        conn: Connection,
        task_id: str,
        *,
        now: str,
        failure_kind: str = "incremental_work_arrived",
    ) -> int:
        """Cancel only queued Plan runs when incremental work takes ownership.

        A pending Plan is a conditional handoff: it may be invalidated by a
        Fact, Todo, assessment, or open Explore Intent arriving before claim.
        Active Plan leases are deliberately excluded so a running panorama
        can late-bind the new input and finish atomically.
        """

        result = conn.execute(
            update(plan_runs)
            .where(
                plan_runs.c.task_id == task_id,
                plan_runs.c.status == "pending",
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
                activity_summary="增量输入到达，排队中的 Plan 已取消并交由 Reason 处理",
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
            select(plan_runs).where(plan_runs.c.status.in_(LEASED_PLAN_STATUSES), plan_runs.c.lease_id.is_not(None), plan_runs.c.last_heartbeat_at.is_not(None), plan_runs.c.last_heartbeat_at <= cutoff)
        ).mappings().all()

    def open(self, conn: Connection, *, task_id: str, plan_run_id: str, lease_id: str, worker: str, context_revision: int, now: str) -> PlanWorkspace:
        workspace_id = allocate_id(conn, scope="global", kind="plan_workspace", prefix="pw", width=6)
        values = {"id": workspace_id, "task_id": task_id, "plan_run_id": plan_run_id, "lease_id": lease_id, "status": "open", "context_revision": context_revision, "draft_json": PlanWorkspaceDraft().model_dump_json(), "worker": worker, "failure_reason": "", "result_json": "{}", "decision_json": "{}", "created_at": now, "updated_at": now, "committed_at": None}
        conn.execute(plan_workspaces.insert().values(**values))
        return self._workspace(values)

    def get(self, conn: Connection, workspace_id: str) -> PlanWorkspace | None:
        row = conn.execute(select(plan_workspaces).where(plan_workspaces.c.id == workspace_id)).mappings().one_or_none()
        return self._workspace(row) if row else None

    def for_attempt(self, conn: Connection, task_id: str, plan_run_id: str, lease_id: str) -> PlanWorkspace | None:
        row = conn.execute(select(plan_workspaces).where(plan_workspaces.c.task_id == task_id, plan_workspaces.c.plan_run_id == plan_run_id, plan_workspaces.c.lease_id == lease_id)).mappings().one_or_none()
        return self._workspace(row) if row else None

    def save_draft(self, conn: Connection, workspace_id: str, draft: PlanWorkspaceDraft, *, now: str) -> PlanWorkspace:
        result = conn.execute(update(plan_workspaces).where(plan_workspaces.c.id == workspace_id, plan_workspaces.c.status == "open").values(draft_json=draft.model_dump_json(), updated_at=now))
        if not result.rowcount:
            raise ValueError("Plan Workspace is not open")
        workspace = self.get(conn, workspace_id)
        assert workspace is not None
        return workspace

    def mark_committed(self, conn: Connection, workspace_id: str, *, created_todo_ids: list[str], updated_todo_ids: list[str], deleted_todo_ids: list[str], created_intent_ids: list[str], decision: dict, now: str) -> PlanWorkspace:
        result_json = json.dumps({"created_todo_ids": created_todo_ids, "updated_todo_ids": updated_todo_ids, "deleted_todo_ids": deleted_todo_ids, "created_intent_ids": created_intent_ids}, ensure_ascii=False)
        result = conn.execute(update(plan_workspaces).where(plan_workspaces.c.id == workspace_id, plan_workspaces.c.status == "open").values(status="committed", result_json=result_json, decision_json=json.dumps(decision, ensure_ascii=False), updated_at=now, committed_at=now))
        if not result.rowcount:
            raise ValueError("Plan Workspace is not open")
        workspace = self.get(conn, workspace_id)
        assert workspace is not None
        return workspace

    def commit_result(self, conn: Connection, workspace: PlanWorkspace) -> dict:
        row = conn.execute(select(plan_workspaces.c.result_json).where(plan_workspaces.c.id == workspace.id)).scalar_one()
        return _json(row, {})

    def abandon(self, conn: Connection, workspace_id: str, *, failure_reason: str, now: str) -> PlanWorkspace:
        result = conn.execute(update(plan_workspaces).where(plan_workspaces.c.id == workspace_id, plan_workspaces.c.status == "open").values(status="abandoned", failure_reason=failure_reason, updated_at=now))
        if not result.rowcount:
            raise ValueError("Plan Workspace is not open")
        workspace = self.get(conn, workspace_id)
        assert workspace is not None
        return workspace

    @staticmethod
    def abandon_open(conn: Connection, *, task_id: str, failure_reason: str, now: str, plan_run_id: str | None = None, lease_id: str | None = None, exclude_workspace_id: str | None = None) -> int:
        conditions = [plan_workspaces.c.task_id == task_id, plan_workspaces.c.status == "open"]
        if plan_run_id is not None:
            conditions.append(plan_workspaces.c.plan_run_id == plan_run_id)
        if lease_id is not None:
            conditions.append(plan_workspaces.c.lease_id == lease_id)
        if exclude_workspace_id is not None:
            conditions.append(plan_workspaces.c.id != exclude_workspace_id)
        result = conn.execute(update(plan_workspaces).where(*conditions).values(status="abandoned", failure_reason=failure_reason, updated_at=now))
        return int(result.rowcount or 0)

    @staticmethod
    def _run(row, source_fact_ids: list[str]) -> PlanRun:
        return PlanRun(
            id=row["id"], task_id=row["task_id"], goal_cycle_id=row.get("goal_cycle_id"), status=row["status"], trigger=row["trigger"],
            trigger_payload=_json(row.get("trigger_payload_json"), {}), input_fingerprint=row["input_fingerprint"], source_fact_ids=source_fact_ids,
            worker_pool=_json(row.get("worker_pool_json"), []), worker=row.get("worker"), settings_version=row["settings_version"], execute_timeout_seconds=row["execute_timeout_seconds"], conclude_timeout_seconds=row["conclude_timeout_seconds"], lease_id=row.get("lease_id"), claimed_at=row.get("claimed_at"), last_heartbeat_at=row.get("last_heartbeat_at"), retry_count=row.get("retry_count", 0), infrastructure_retry_count=row.get("infrastructure_retry_count", 0), next_retry_at=row.get("next_retry_at"), last_failure_kind=row.get("last_failure_kind") or "", failure_class=row.get("failure_class") or "", activity_kind=row.get("activity_kind") or "", activity_summary=row.get("activity_summary") or "", activity_tool=row.get("activity_tool") or "", activity_started_at=row.get("activity_started_at"), activity_updated_at=row.get("activity_updated_at"), created_at=row["created_at"], updated_at=row["updated_at"], concluded_at=row.get("concluded_at"), decision=_json(row.get("decision_json"), {}),
        )

    @staticmethod
    def _workspace(row) -> PlanWorkspace:
        return PlanWorkspace(id=row["id"], task_id=row["task_id"], plan_run_id=row["plan_run_id"], lease_id=row["lease_id"], status=row["status"], context_revision=row["context_revision"], draft=PlanWorkspaceDraft.model_validate_json(row.get("draft_json") or "{}"), worker=row["worker"], failure_reason=row.get("failure_reason") or "", result=_json(row.get("result_json"), {}), decision=_json(row.get("decision_json"), {}), created_at=row["created_at"], updated_at=row["updated_at"], committed_at=row.get("committed_at"))
