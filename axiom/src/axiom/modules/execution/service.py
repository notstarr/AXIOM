from __future__ import annotations

import json
import logging

from datetime import timedelta

from sqlalchemy import Engine, update

from axiom.modules.artifacts.repository import ArtifactRepository
from axiom.modules.audit.schemas import RecordAuditEvent
from axiom.modules.audit.service import AuditService
from axiom.modules.context.service import ContextService
from axiom.modules.credentials.repository import CredentialRepository
from axiom.modules.credentials.schemas import (
    CredentialMaterial,
    WorkerCredentialRefresh,
    WorkerCredentialSummary,
)
from axiom.modules.execution.repository import ExecutionRepository
from axiom.modules.execution.schemas import (
    ActivityCommand,
    BootstrapCompleted,
    CompleteBootstrapCommand,
    CompleteExploreCommand,
    CredentialAccessCommand,
    ExecutionInput,
    ExploreCompleted,
    ExploreVulnerabilityRecorded,
    HumanInterventionMarker,
    LeaseCommand,
    MarkHumanInterventionCommand,
    ReportExploreVulnerabilityCommand,
    ReleaseIntentCommand,
    ReleasedIntent,
    StartedIntent,
)
from axiom.modules.execution.stage_outputs import (
    EXPLORE_FINDINGS_FILE,
    append_explore_vulnerability_locked,
    bootstrap_files,
    explore_state_guard,
    explore_files,
    finalize_explore_state,
    load_explore_state,
    write_bootstrap_index,
)
from axiom.modules.graph.tables import facts
from axiom.modules.axiom_tools.service import AxiomToolService
from axiom.modules.reason.service import ReasonService
from axiom.modules.settings.service import load_runtime_config
from axiom.modules.tasks.schemas import TaskScope
from axiom.modules.workers.tables import workers
from axiom.modules.skills.service import SkillService
from axiom.modules.vulnerabilities.repository import VulnerabilityRepository
from axiom.modules.vulnerabilities.schemas import StoredExploreVulnerability, VulnerabilityReport
from axiom.modules.vulnerability_reviews.service import VulnerabilityReviewService
from axiom.shared.clock import format_utc, parse_utc, utcnow
from axiom.shared.database import transaction
from axiom.shared.errors import ConflictError, LeaseInactiveError, NotFoundError
from axiom.shared.ids import allocate_id
from axiom.shared.storage import FileStore


logger = logging.getLogger(__name__)


_REASON_PROMPT_PROFILE_BY_TRIGGER = {
    "bootstrap": "bootstrap",
    "fact_batch": "progress",
    "low_watermark": "progress",
    "max_wait": "progress",
    "intent_assessment": "progress",
    "manual": "human",
    "hint": "human",
    "continuation": "continuation",
    "plan": "plan",
}


def reason_prompt_profile_for_trigger(trigger: str) -> str:
    """Return the one code-selected prompt profile for a ReasonRun trigger."""

    return _REASON_PROMPT_PROFILE_BY_TRIGGER.get(trigger, "default")


def _scope_payload(scope: TaskScope) -> dict[str, object]:
    return scope.model_dump(mode="json") if scope.enabled else {}


class ExecutionService:
    def __init__(
        self,
        engine: Engine,
        store: FileStore,
        context: ContextService,
        reason: ReasonService,
        skills: SkillService,
        axiom_tools: AxiomToolService,
        reviews: VulnerabilityReviewService,
    ):
        self.engine = engine
        self.store = store
        self.repository = ExecutionRepository()
        self.artifacts = ArtifactRepository()
        self.context = context
        self.reason = reason
        self.skills = skills
        self.axiom_tools = axiom_tools
        self.phase_capabilities = axiom_tools.phase_capabilities
        self.credentials = CredentialRepository()
        self.vulnerabilities = VulnerabilityRepository()
        self.reviews = reviews
        self.audit = AuditService(engine)

    def credential_summaries(
        self,
        *,
        task_id: str,
        command: CredentialAccessCommand,
    ) -> list[WorkerCredentialSummary]:
        with self.engine.connect() as conn:
            project_id = self._credential_project_id(conn, task_id, command)
            credentials_for_project = self.credentials.list(
                conn,
                project_id,
                enabled_only=True,
            )
        return [
            WorkerCredentialSummary(
                id=item.id,
                name=item.name,
            )
            for item in credentials_for_project
        ]

    def credential_material(
        self,
        *,
        task_id: str,
        credential_id: str,
        command: CredentialAccessCommand,
    ) -> CredentialMaterial:
        with self.engine.connect() as conn:
            project_id = self._credential_project_id(conn, task_id, command)
            credential = self.credentials.get(conn, project_id, credential_id)
        if credential is None or not credential.enabled:
            raise NotFoundError(f"Enabled credential not found: {credential_id}")
        return CredentialMaterial(
            id=credential.id,
            name=credential.name,
            content=credential.content,
            notes=credential.notes,
            refresh_id=credential.refresh_id,
        )

    def credential_refresh_material(
        self,
        *,
        task_id: str,
        refresh_id: str,
        command: CredentialAccessCommand,
    ) -> WorkerCredentialRefresh:
        with self.engine.connect() as conn:
            project_id = self._credential_project_id(conn, task_id, command)
            refresh = self.credentials.get_refresh(
                conn,
                project_id,
                refresh_id,
                enabled_only=True,
            )
        if refresh is None:
            raise NotFoundError(
                f"Enabled credential refresh not found: {refresh_id}"
            )
        return WorkerCredentialRefresh(id=refresh.id, content=refresh.content)

    def input(self, *, task_id: str, intent_id: str, command: LeaseCommand) -> ExecutionInput:
        with self.engine.connect() as conn:
            row = self.repository.input_row(conn, task_id, intent_id)
            if row is None:
                raise NotFoundError(f"Intent not found: {task_id}/{intent_id}")
            self._require_lease(row, command.lease_id)
            if row["task_status"] != "running":
                raise LeaseInactiveError("Task is not running")
            if row["intent_status"] not in {"claimed", "running", "concluding"}:
                raise LeaseInactiveError("Intent lease is not active")
            execute_timeout = row["execute_timeout_seconds"]
            conclude_timeout = row["conclude_timeout_seconds"]
            if row["kind"] == "bootstrap":
                if execute_timeout is None or conclude_timeout is None:
                    raise ConflictError("Bootstrap budget snapshot is missing; release and reclaim it")
            values = {
                "worker_name": row["worker"] or "",
                "intent_kind": row["kind"],
                "intent_description": row["intent_description"],
                "intent_targets": json.loads(row["targets_json"] or "[]"),
                "intent_source_facts": self.repository.source_facts(conn, task_id, intent_id),
                "todo_id": row["todo_id"],
                "origin": row["origin_input"],
                "goal": row["goal"],
                "hints": self.repository.hints(conn, task_id),
                "scope": self.repository.parse_scope(row),
                "execute_timeout_seconds": execute_timeout,
                "conclude_timeout_seconds": conclude_timeout,
                "phase": row["kind"],
            }
        context = self.context.build(
            task_id=task_id,
            phase=values["phase"],
            intent_id=intent_id,
            lease_id=command.lease_id,
        )
        run_dir = str(
            self.store.run_directory(
                task_id=task_id,
                intent_id=intent_id,
                lease_id=command.lease_id,
            )
        )
        capabilities = self.phase_capabilities.snapshot_for_lease(
            worker_name=values["worker_name"],
            phase=values["phase"],
            run_dir=run_dir,
        )
        return ExecutionInput(
            task_id=task_id,
            project_id=row["project_id"],
            intent_id=intent_id,
            lease_id=command.lease_id,
            worker_name=values["worker_name"],
            intent_kind=values["intent_kind"],
            intent_description=values["intent_description"],
            intent_targets=values["intent_targets"],
            intent_source_facts=values["intent_source_facts"],
            todo_id=values["todo_id"],
            origin=values["origin"],
            goal=values["goal"],
            hints=values["hints"],
            skill_directories=capabilities.skill_directories,
            mcp_tool_names=capabilities.mcp_tool_names,
            capability_environment=capabilities.mcp_environment,
            capability_snapshot_path=capabilities.capability_snapshot_path,
            guard_enabled=bool(row["guard_enabled"]),
            scope=values["scope"],
            run_dir=run_dir,
            execute_timeout_seconds=values["execute_timeout_seconds"],
            conclude_timeout_seconds=values["conclude_timeout_seconds"],
            context_revision=context.revision,
            context_manifest_path=context.manifest_path,
            axiom_tool_keys=capabilities.axiom_tool_keys,
            axiom_tool_extensions=capabilities.axiom_tool_extensions,
            axiom_tool_snapshot_path=capabilities.axiom_tool_snapshot_path,
        )

    def reason_input(
        self,
        *,
        task_id: str,
        reason_run_id: str,
        command: LeaseCommand,
    ) -> ExecutionInput:
        with self.engine.connect() as conn:
            run = self.reason.repository.get_run(conn, task_id, reason_run_id)
            if run is None:
                raise NotFoundError(f"ReasonRun not found: {task_id}/{reason_run_id}")
            if run.lease_id != command.lease_id:
                raise LeaseInactiveError("ReasonRun lease does not match")
            if run.status not in {"claimed", "running", "concluding"}:
                raise LeaseInactiveError("ReasonRun lease is not active")
            task = self.reason.tasks.get(conn, task_id)
            if task is None:
                raise NotFoundError(f"Task not found: {task_id}")
            if task.status != "running":
                raise LeaseInactiveError("Task is not running")
            hints = self.repository.hints(conn, task_id)
            goal_cycle = (
                self.reason.goals.get_cycle(conn, task_id, task.current_goal_cycle_id)
                if task.current_goal_cycle_id
                else None
            )

        context_phase = "plan" if run.trigger == "plan" else "reason"
        context = self.context.build(
            task_id=task_id,
            phase=context_phase,
            intent_id=None,
            reason_run_id=reason_run_id,
            lease_id=command.lease_id,
        )
        trigger_summary = {
            "bootstrap": "分析 Bootstrap 初始攻击面并规划第一批 Explore Intent",
            "fact_batch": "消费新 Fact 批次并规划后续 Explore Intent",
            "low_watermark": "补充即将耗尽的 Explore 工作队列",
            "max_wait": "处理等待时间达到上限的新 Fact",
        }.get(run.trigger, f"根据触发原因 {run.trigger} 增量规划下一轮测试")
        goal_cycle_guidance = ""
        if goal_cycle is not None and goal_cycle.sequence > 1:
            goal_cycle_guidance = (
                f"当前 Goal Cycle 的人工续跑原因：{goal_cycle.continuation_reason or '未说明'}。"
                f"本轮重点：{goal_cycle.focus or '全面复盘'}。"
                "该人工说明是有效的规划依据，可以结合已有 findings/leads 恢复仍有材料性工作的终态 Todo，"
                "即使本轮没有新 Fact；但它不是 Fact，不能伪造为 Todo source、Intent from_fact 或 Goal evidence。"
                "请依据当前 Goal 和原始 Fact、Todo、Intent、Artifact、漏洞独立判断，不要寻找或继承历史 Goal Evaluation。"
            )
        if run.trigger == "manual":
            payload = run.trigger_payload
            trigger_summary = (
                f"人工 {payload.get('actor') or '操作员'} 请求立即复盘当前任务。"
                "请结合最新 Context 全面检查新 Fact、Todo 收口、失败及开放 Explore 和 Goal；"
                "不得为了响应人工操作而凑数派发，没有材料性新增方向时允许新增 0 个 Intent。"
                f"触发快照：新 Fact {payload.get('unplanned_fact_count', 0)} 个，"
                f"开放 Explore {payload.get('open_explore_count', 0)} 个，"
                f"失败/取消 Explore {len(payload.get('failed_intent_ids') or [])} 个，"
                f"待处理 Todo {len(payload.get('actionable_todo_ids') or [])} 个，"
                f"待收口 Todo {len(payload.get('todo_review_ids') or [])} 个。"
            )
        elif run.trigger == "hint":
            payload = run.trigger_payload
            trigger_summary = (
                f"人工 {payload.get('creator') or '操作员'} 新增了一条提示并触发本轮 Reason。"
                f"本轮首要触发原因（Hint {payload.get('hint_id') or '未知'}）："
                f"{payload.get('hint_content') or '未提供内容'}。"
                "请优先围绕这条提示复盘现有 Fact、Todo、Intent、Artifact、漏洞和开放 Explore，"
                "据此调整规划；Hint 是人工规划依据而不是已验证 Fact，不得伪造为 Todo source、"
                "Intent from_fact 或 Goal evidence。"
                f"触发快照：新 Fact {payload.get('unplanned_fact_count', 0)} 个，"
                f"开放 Explore {payload.get('open_explore_count', 0)} 个，"
                f"失败/取消 Explore {len(payload.get('failed_intent_ids') or [])} 个，"
                f"待处理 Todo {len(payload.get('actionable_todo_ids') or [])} 个，"
                f"待收口 Todo {len(payload.get('todo_review_ids') or [])} 个。"
            )
        elif run.trigger == "continuation":
            payload = run.trigger_payload
            continuation_reason = (
                goal_cycle.continuation_reason
                if goal_cycle is not None
                else payload.get("continuation_reason") or ""
            )
            focus = goal_cycle.focus if goal_cycle is not None else payload.get("focus") or ""
            trigger_summary = (
                f"人工 {payload.get('actor') or '操作员'} 已将已完成任务续跑。"
                f"当前唯一 Goal：{task.goal}。"
                f"人工续跑原因：{continuation_reason or '未说明'}。"
                f"本轮重点：{focus or '全面复盘'}。"
                "请复用历史 Fact、Todo、Intent、Artifact 和漏洞，但只评估当前 Goal；"
                "不要自动恢复旧的 cancelled Intent，应按当前 Goal 选择性复用 Todo并提出新 Intent。"
                "历史 Goal、Goal Evaluation 及其证据不会作为本轮上下文提供，"
                "必须依据当前可见的原始事实和产物独立判断；人工续跑说明/重点可以作为恢复 Todo 和规划新 Intent 的依据，"
                "没有材料性方向时才允许新增 0 个 Intent。"
            )
        elif run.trigger == "intent_assessment":
            payload = run.trigger_payload
            intent_ids = payload.get("unassessed_intent_ids") or []
            trigger_summary = (
                "代码发现以下已结束的 Explore Intent 尚未完成一次性评估："
                f"{', '.join(str(item) for item in intent_ids) or '未提供'}。"
                "请先逐项读取这些 Intent 的 findings、leads、输出 Fact 和执行结果，"
                "对每个 Intent 只调用一次 `axiom reason intent assess`，状态只能是 partial、blocked 或 completed，"
                "并写明具体理由；若仍需继续测试，保留原评估并创建关联相关 Fact 的新 Intent。"
                "评估本身不消费 Fact，仍需通过 Todo source、后续 Intent from_fact 或 Goal evidence 引用新 Fact。"
            )
        elif run.trigger == "plan":
            payload = run.trigger_payload
            trigger_summary = (
                "任务的 Reason 与 Explore 增量队列已经耗尽，本轮进入 Plan 全景复盘。"
                "请从全部历史 Todo、Intent、Fact、Hint、Knowledge、Artifact、漏洞和 Goal Cycle 中检查"
                "覆盖完整性、证据武器化、阻断路径替代方案与目标完成度。"
                "不要机械凑任务；只有存在可引用证据且与历史测试不重复时才恢复 Todo 或派发 Explore Intent。"
                "若没有材料性新方向，保持 Goal 未达成并提交可审计的穷尽理由，让任务安全进入 waiting。"
                f"本 Goal Cycle 的连续 Plan 序号：{payload.get('plan_sequence', 1)}。"
            )
        if goal_cycle_guidance:
            trigger_summary += goal_cycle_guidance
        run_dir = str(
            self.store.run_directory(
                task_id=task_id,
                reason_run_id=reason_run_id,
                lease_id=command.lease_id,
            )
        )
        capabilities = self.phase_capabilities.snapshot_for_lease(
            worker_name=run.worker or "",
            phase="reason",
            run_dir=run_dir,
        )
        return ExecutionInput(
            task_id=task_id,
            project_id=task.project_id,
            intent_id=None,
            reason_run_id=reason_run_id,
            lease_id=command.lease_id,
            worker_name=run.worker or "",
            intent_kind="reason",
            reason_prompt_profile=reason_prompt_profile_for_trigger(run.trigger),
            intent_description=trigger_summary,
            intent_targets=[],
            intent_source_facts=run.source_fact_ids,
            todo_id=None,
            origin=task.origin.original_input,
            goal=task.goal,
            hints=hints,
            skill_directories=capabilities.skill_directories,
            mcp_tool_names=capabilities.mcp_tool_names,
            capability_environment=capabilities.mcp_environment,
            capability_snapshot_path=capabilities.capability_snapshot_path,
            guard_enabled=task.guard_enabled,
            scope=_scope_payload(task.scope),
            run_dir=run_dir,
            execute_timeout_seconds=run.execute_timeout_seconds,
            conclude_timeout_seconds=run.conclude_timeout_seconds,
            context_revision=context.revision,
            context_manifest_path=context.manifest_path,
            axiom_tool_keys=capabilities.axiom_tool_keys,
            axiom_tool_extensions=capabilities.axiom_tool_extensions,
            axiom_tool_snapshot_path=capabilities.axiom_tool_snapshot_path,
        )

    def review_input(
        self,
        *,
        task_id: str,
        review_run_id: str,
        command: LeaseCommand,
    ) -> ExecutionInput:
        with self.engine.connect() as conn:
            run = self.reviews.repository.get(conn, task_id, review_run_id)
            if run is None:
                raise NotFoundError(
                    f"Vulnerability ReviewRun not found: {task_id}/{review_run_id}"
                )
            if run.lease_id != command.lease_id:
                raise LeaseInactiveError("Vulnerability ReviewRun lease does not match")
            if run.status not in {"claimed", "running", "concluding"}:
                raise LeaseInactiveError("Vulnerability ReviewRun lease is not active")
            task = self.reason.tasks.get(conn, task_id)
            if task is None:
                raise NotFoundError(f"Task not found: {task_id}")
            if task.legacy_readonly:
                raise LeaseInactiveError("Legacy Task is read-only")
            vulnerability = self.vulnerabilities.get(conn, task_id, run.vulnerability_id)
            if vulnerability is None or vulnerability.archived:
                raise LeaseInactiveError("Vulnerability is no longer reviewable")
            hints = self.repository.hints(conn, task_id)

        context = self.context.build(
            task_id=task_id,
            phase="review",
            intent_id=None,
            review_run_id=review_run_id,
            lease_id=command.lease_id,
        )
        trigger_summary = {
            "reported": "审核新上报的待验证漏洞",
            "updated": "关键 Finding、Evidence 或漏洞字段已更新，重新审核",
            "manual": "人工要求重新审核该漏洞",
            "restored": "归档恢复后重新审核该待验证漏洞",
            "migration": "审核部署前已有的待验证漏洞",
        }.get(run.trigger, f"根据触发原因 {run.trigger} 审核该漏洞")
        manual_reason = str(run.trigger_payload.get("reason") or "").strip()
        if manual_reason:
            trigger_summary += f"。人工说明：{manual_reason}"
        run_dir = str(
            self.store.run_directory(
                task_id=task_id,
                review_run_id=review_run_id,
                lease_id=command.lease_id,
            )
        )
        capabilities = self.phase_capabilities.snapshot_for_lease(
            worker_name=run.worker or "",
            phase="review",
            run_dir=run_dir,
        )
        targets = [vulnerability.affected_target] if vulnerability.affected_target else []
        return ExecutionInput(
            task_id=task_id,
            project_id=task.project_id,
            review_run_id=review_run_id,
            vulnerability_id=vulnerability.id,
            lease_id=command.lease_id,
            worker_name=run.worker or "",
            intent_kind="review",
            intent_description=trigger_summary,
            intent_targets=targets,
            intent_source_facts=[],
            origin=task.origin.original_input,
            goal=task.goal,
            hints=hints,
            skill_directories=capabilities.skill_directories,
            mcp_tool_names=capabilities.mcp_tool_names,
            capability_environment=capabilities.mcp_environment,
            capability_snapshot_path=capabilities.capability_snapshot_path,
            guard_enabled=task.guard_enabled,
            scope=_scope_payload(task.scope),
            run_dir=run_dir,
            execute_timeout_seconds=run.execute_timeout_seconds,
            conclude_timeout_seconds=run.conclude_timeout_seconds,
            context_revision=context.revision,
            context_manifest_path=context.manifest_path,
            axiom_tool_keys=capabilities.axiom_tool_keys,
            axiom_tool_extensions=capabilities.axiom_tool_extensions,
            axiom_tool_snapshot_path=capabilities.axiom_tool_snapshot_path,
        )

    def start(self, *, task_id: str, intent_id: str, command: LeaseCommand) -> StartedIntent:
        now = utcnow()
        with transaction(self.engine) as conn:
            row = self.repository.input_row(conn, task_id, intent_id)
            self._require_row(row, task_id, intent_id)
            self._require_lease(row, command.lease_id)
            if row["task_status"] != "running":
                raise LeaseInactiveError("Task is not running")
            if row["intent_status"] == "running":
                return StartedIntent(
                    task_id=task_id,
                    intent_id=intent_id,
                    worker_name=row["worker"],
                    lease_id=command.lease_id,
                )
            if row["intent_status"] != "claimed":
                raise ConflictError("Intent is not in claimed state")
            if not self.repository.transition_to_running(
                conn,
                task_id=task_id,
                intent_id=intent_id,
                lease_id=command.lease_id,
                now=now,
            ):
                raise ConflictError("Intent start lost a concurrent race")
            self.audit.record_in(
                conn,
                RecordAuditEvent(
                    project_id=row["project_id"],
                    task_id=task_id,
                    intent_id=intent_id,
                    worker=row["worker"] or "",
                    event_type="intent.started",
                    message=f"{row['kind']} Intent 开始执行",
                    payload={"phase": row["kind"], "lease_id": command.lease_id},
                ),
            )
            return StartedIntent(
                task_id=task_id,
                intent_id=intent_id,
                worker_name=row["worker"],
                lease_id=command.lease_id,
            )

    def heartbeat(self, *, task_id: str, intent_id: str, command: LeaseCommand) -> None:
        with transaction(self.engine) as conn:
            if not self.repository.heartbeat(
                conn,
                task_id=task_id,
                intent_id=intent_id,
                lease_id=command.lease_id,
                now=utcnow(),
            ):
                raise LeaseInactiveError("Intent lease is no longer active")

    def activity(self, *, task_id: str, intent_id: str, command: ActivityCommand) -> None:
        with transaction(self.engine) as conn:
            row = self.repository.input_row(conn, task_id, intent_id)
            self._require_row(row, task_id, intent_id)
            self._require_lease(row, command.lease_id)
            if row["task_status"] != "running":
                raise LeaseInactiveError("Task is not running")
            if row["intent_status"] not in {"claimed", "running", "concluding"}:
                raise LeaseInactiveError("Intent lease is not active")
            if not self.repository.update_activity(
                conn,
                task_id=task_id,
                intent_id=intent_id,
                lease_id=command.lease_id,
                activity_kind=command.activity_kind,
                activity_summary=command.activity_summary,
                activity_tool=command.activity_tool,
                now=utcnow(),
            ):
                raise LeaseInactiveError("Intent lease is no longer active")

    def mark_human_intervention(
        self,
        *,
        task_id: str,
        intent_id: str,
        command: MarkHumanInterventionCommand,
    ) -> HumanInterventionMarker:
        with transaction(self.engine) as conn:
            row = self.repository.input_row(conn, task_id, intent_id)
            self._require_row(row, task_id, intent_id)
            self._require_lease(row, command.lease_id)
            if row["task_status"] != "running":
                raise LeaseInactiveError("Task is not running")
            if row["kind"] != "explore":
                raise ConflictError("Intent is not an Explore Intent")
            if row["intent_status"] not in {"claimed", "running", "concluding"}:
                raise LeaseInactiveError("Explore Intent lease is not active")

            reason = command.reason
            unchanged = (
                bool(row["human_intervention_required"])
                and row["human_intervention_reason"] == reason
                and bool(row["human_intervention_marked_at"])
            )
            if unchanged:
                return HumanInterventionMarker(
                    task_id=task_id,
                    intent_id=intent_id,
                    reason=reason,
                    marked_at=row["human_intervention_marked_at"],
                    updated=False,
                )

            now = utcnow()
            if not self.repository.mark_human_intervention(
                conn,
                task_id=task_id,
                intent_id=intent_id,
                lease_id=command.lease_id,
                reason=reason,
                now=now,
            ):
                raise LeaseInactiveError("Explore Intent lease is no longer active")
            self.audit.record_in(
                conn,
                RecordAuditEvent(
                    project_id=row["project_id"],
                    task_id=task_id,
                    intent_id=intent_id,
                    worker=row["worker"] or "",
                    event_type="intent.human_intervention_marked",
                    level="warning",
                    message="Explore Intent 标记为需要人工介入",
                    payload={
                        "phase": "explore",
                        "reason": reason,
                    },
                ),
            )
            return HumanInterventionMarker(
                task_id=task_id,
                intent_id=intent_id,
                reason=reason,
                marked_at=now,
                updated=True,
            )

    def begin_conclude(self, *, task_id: str, intent_id: str, command: LeaseCommand) -> None:
        with transaction(self.engine) as conn:
            row = self.repository.input_row(conn, task_id, intent_id)
            self._require_row(row, task_id, intent_id)
            self._require_lease(row, command.lease_id)
            if not self.repository.transition_to_concluding(
                conn,
                task_id=task_id,
                intent_id=intent_id,
                lease_id=command.lease_id,
                now=utcnow(),
            ):
                raise ConflictError("Intent is not running under this lease")
            self.audit.record_in(
                conn,
                RecordAuditEvent(
                    project_id=row["project_id"],
                    task_id=task_id,
                    intent_id=intent_id,
                    worker=row["worker"] or "",
                    event_type="intent.concluding",
                    message=f"{row['kind']} Intent 进入收口阶段",
                    payload={"phase": row["kind"], "lease_id": command.lease_id},
                ),
            )

    def release(
        self,
        *,
        task_id: str,
        intent_id: str,
        command: ReleaseIntentCommand,
    ) -> ReleasedIntent:
        now = utcnow()
        with transaction(self.engine) as conn:
            row = self.repository.input_row(conn, task_id, intent_id)
            self._require_row(row, task_id, intent_id)
            self._require_lease(row, command.lease_id)
            config = load_runtime_config(conn)
            next_count = (
                row["retry_count"] + 1
                if command.failure_class == "execution"
                else row["infrastructure_retry_count"] + 1
            )
            limit = (
                config.execution_retry_limit
                if command.failure_class == "execution"
                else config.infrastructure_retry_limit
            )
            terminal = next_count >= limit
            delay = min(
                config.retry_max_seconds,
                config.retry_base_seconds * (2 ** max(0, next_count - 1)),
            )
            next_retry_at = (
                format_utc(parse_utc(now) + timedelta(seconds=delay)) if not terminal else None
            )
            if not self.repository.release(
                conn,
                task_id=task_id,
                intent_id=intent_id,
                lease_id=command.lease_id,
                failure_kind=command.failure_kind,
                failure_class=command.failure_class,
                next_retry_at=next_retry_at,
                terminal=terminal,
                now=now,
            ):
                raise ConflictError("Intent lease is no longer active")
            self.audit.record_in(
                conn,
                RecordAuditEvent(
                    project_id=row["project_id"],
                    task_id=task_id,
                    intent_id=intent_id,
                    worker=row["worker"] or "",
                    event_type="intent.released",
                    level="warning" if command.failure_class == "transient" else "info",
                    message=f"{row['kind']} Intent 已释放：{command.failure_kind or '未指定原因'}",
                    payload={
                        "phase": row["kind"],
                        "failure_kind": command.failure_kind,
                        "failure_class": command.failure_class,
                        "terminal": terminal,
                        "next_retry_at": next_retry_at,
                    },
                ),
            )
            if command.failure_class == "transient" and row["worker"]:
                cooldown = format_utc(
                    parse_utc(now) + timedelta(seconds=config.worker_failure_cooldown_seconds)
                )
                conn.execute(
                    update(workers)
                    .where(workers.c.name == row["worker"])
                    .values(cooldown_until=cooldown, updated_at=now)
                )
        return ReleasedIntent(
            task_id=task_id,
            intent_id=intent_id,
            status="failed" if terminal else "pending",
            next_retry_at=next_retry_at,
            retry_count=row["retry_count"] + (1 if command.failure_class == "execution" else 0),
            infrastructure_retry_count=row["infrastructure_retry_count"]
            + (1 if command.failure_class == "transient" else 0),
        )

    def complete_bootstrap(
        self,
        *,
        task_id: str,
        intent_id: str,
        command: CompleteBootstrapCommand,
    ) -> BootstrapCompleted:
        with self.engine.connect() as conn:
            existing = self.repository.input_row(conn, task_id, intent_id)
            self._require_row(existing, task_id, intent_id)
            self._require_lease(existing, command.lease_id)
            if existing["intent_status"] == "completed" and existing["to_fact_id"]:
                return self._completed(conn, task_id, intent_id, existing["to_fact_id"])

        run_dir = self.store.run_directory(
            task_id=task_id,
            intent_id=intent_id,
            lease_id=command.lease_id,
        )
        outputs = bootstrap_files(run_dir)
        prepared = self.store.prepare_existing_files(
            base_key=f"tasks/{task_id}/artifacts/{intent_id}",
            files={item.relative_name: item.path for item in outputs},
        )
        now = utcnow()
        transaction_committed = False
        try:
            with transaction(self.engine) as conn:
                row = self.repository.input_row(conn, task_id, intent_id)
                self._require_row(row, task_id, intent_id)
                self._require_lease(row, command.lease_id)
                if row["task_status"] != "running":
                    raise ConflictError("Task is not running")
                if row["kind"] != "bootstrap":
                    raise ConflictError("Intent is not a Bootstrap Intent")
                if row["intent_status"] not in {"running", "concluding"}:
                    if row["intent_status"] == "completed" and row["to_fact_id"]:
                        result = self._completed(conn, task_id, intent_id, row["to_fact_id"])
                        prepared.finish()
                        return result
                    raise ConflictError("Intent is not ready for completion")

                prepared.commit()
                records = self.artifacts.create_bootstrap_bundle(
                    conn,
                    task_id=task_id,
                    intent_id=intent_id,
                    prepared=prepared.files,
                    created_by=row["worker"] or "bootstrap",
                    now=now,
                )
                fact_id = allocate_id(conn, scope=task_id, kind="fact", prefix="f")
                conn.execute(
                    facts.insert().values(
                        id=fact_id,
                        task_id=task_id,
                        description=self._bootstrap_fact(records),
                        created_at=now,
                        archived=False,
                    )
                )
                if not self.repository.complete_intent(
                    conn,
                    task_id=task_id,
                    intent_id=intent_id,
                    lease_id=command.lease_id,
                    fact_id=fact_id,
                    now=now,
                ):
                    raise ConflictError("Intent completion lost a concurrent race")
                self.audit.record_in(
                    conn,
                    RecordAuditEvent(
                        project_id=row["project_id"],
                        task_id=task_id,
                        intent_id=intent_id,
                        worker=row["worker"] or "",
                        event_type="intent.completed",
                        message="Bootstrap 已完成，已写入资产与初始线索",
                        payload={"phase": "bootstrap", "fact_id": fact_id, "artifact_count": len(records)},
                    ),
                )
                next_reason_run_id = self.reason.create_initial(
                    conn,
                    task_id=task_id,
                    source_fact_id=fact_id,
                    now=now,
                )
                self.repository.complete_task_phase(
                    conn,
                    task_id,
                    now,
                    phase="reason" if next_reason_run_id else "waiting",
                )
            transaction_committed = True
            try:
                prepared.finish()
            except OSError:
                # The durable Artifact and database transaction already won;
                # retaining a worker-run copy is safer than deleting the only
                # registered file during cleanup.
                logger.exception("failed to clean Bootstrap run outputs: %s/%s", task_id, intent_id)
            result = BootstrapCompleted(
                task_id=task_id,
                intent_id=intent_id,
                fact_id=fact_id,
                next_reason_run_id=next_reason_run_id,
                artifacts=records,
            )
            try:
                write_bootstrap_index(run_dir, records)
            except Exception:
                logger.exception("failed to write Bootstrap internal index: %s/%s", task_id, intent_id)
            return result
        except BaseException:
            if not transaction_committed:
                prepared.rollback()
            raise

    def complete_explore(
        self,
        *,
        task_id: str,
        intent_id: str,
        command: CompleteExploreCommand,
    ) -> ExploreCompleted:
        with self.engine.connect() as conn:
            existing = self.repository.input_row(conn, task_id, intent_id)
            self._require_row(existing, task_id, intent_id)
            self._require_lease(existing, command.lease_id)
            if existing["intent_status"] == "completed" and existing["to_fact_id"]:
                return self._explore_completed(conn, task_id, intent_id, existing["to_fact_id"])

        run_dir = self.store.run_directory(
            task_id=task_id,
            intent_id=intent_id,
            lease_id=command.lease_id,
        )
        outputs = explore_files(run_dir)
        prepared = self.store.prepare_existing_files(
            base_key=f"tasks/{task_id}/artifacts/{intent_id}",
            files={item.relative_name: item.path for item in outputs},
        )
        specs = {
            item.relative_name: (item.artifact_type, item.summary)
            for item in outputs
        }
        now = utcnow()
        transaction_committed = False
        try:
            with explore_state_guard(run_dir):
                state = load_explore_state(run_dir)
                available_outputs = {item.relative_name for item in outputs}
                with transaction(self.engine) as conn:
                    row = self.repository.input_row(conn, task_id, intent_id)
                    self._require_row(row, task_id, intent_id)
                    self._require_lease(row, command.lease_id)
                    if row["task_status"] != "running":
                        raise ConflictError("Task is not running")
                    if row["kind"] != "explore":
                        raise ConflictError("Intent is not an Explore Intent")
                    if row["intent_status"] not in {"running", "concluding"}:
                        if row["intent_status"] == "completed" and row["to_fact_id"]:
                            result = self._explore_completed(
                                conn,
                                task_id,
                                intent_id,
                                row["to_fact_id"],
                            )
                            prepared.finish()
                            return result
                        raise ConflictError("Intent is not ready for completion")

                    prepared.commit()
                    records = self.artifacts.create_explore_bundle(
                        conn,
                        task_id=task_id,
                        intent_id=intent_id,
                        prepared=prepared.files,
                        specs=specs,
                        created_by=row["worker"] or "explore",
                        now=now,
                    )
                    artifact_by_relative = {
                        item.relative_name: record
                        for item, record in zip(prepared.files, records, strict=True)
                    }
                    finding_artifact = artifact_by_relative[EXPLORE_FINDINGS_FILE]
                    vulnerability_records = []
                    for report in state.vulnerabilities:
                        missing_evidence_paths = [
                            path
                            for path in report.evidence_paths
                            if path not in available_outputs
                        ]
                        if missing_evidence_paths:
                            logger.warning(
                                "explore_vulnerability_evidence_skipped task_id=%s "
                                "intent_id=%s lease_id=%s title=%r count=%s paths=%r",
                                task_id,
                                intent_id,
                                command.lease_id,
                                report.title,
                                len(missing_evidence_paths),
                                missing_evidence_paths,
                            )
                        evidence_artifact_ids = [
                            artifact_by_relative[path].id
                            for path in report.evidence_paths
                            if path in artifact_by_relative
                            and artifact_by_relative[path].artifact_type
                            == "explore.evidence"
                        ]
                        vulnerability = self.vulnerabilities.create(
                            conn,
                            task_id=task_id,
                            body=VulnerabilityReport(
                                **report.model_dump(
                                    mode="python",
                                    exclude={"evidence_paths"},
                                ),
                                status="待验证",
                                status_reason="",
                                finding_artifact_id=finding_artifact.id,
                                evidence_artifact_ids=evidence_artifact_ids,
                            ),
                            now=now,
                        )
                        vulnerability_records.append(vulnerability)
                        self.reviews.enqueue_in(
                            conn,
                            task_id=task_id,
                            vulnerability_id=vulnerability.id,
                            trigger="reported",
                            trigger_payload={
                                "source": "explore",
                                "intent_id": intent_id,
                            },
                            now=now,
                        )
                    fact_id = allocate_id(conn, scope=task_id, kind="fact", prefix="f")
                    conn.execute(
                        facts.insert().values(
                            id=fact_id,
                            task_id=task_id,
                            description=self._explore_fact(
                                intent_description=row["intent_description"],
                                artifacts=records,
                                vulnerability_ids=[item.id for item in vulnerability_records],
                            ),
                            created_at=now,
                            archived=False,
                        )
                    )
                    if not self.repository.complete_intent(
                        conn,
                        task_id=task_id,
                        intent_id=intent_id,
                        lease_id=command.lease_id,
                        fact_id=fact_id,
                        now=now,
                    ):
                        raise ConflictError("Intent completion lost a concurrent race")
                    reason_run_id = self.reason.advance_after_explore(
                        conn,
                        task_id=task_id,
                        now=now,
                    )
                    self.audit.record_in(
                        conn,
                        RecordAuditEvent(
                            project_id=row["project_id"],
                            task_id=task_id,
                            intent_id=intent_id,
                            worker=row["worker"] or "",
                            event_type="intent.completed",
                            message="Explore 已完成并写入 Fact",
                            payload={
                                "phase": "explore",
                                "fact_id": fact_id,
                                "artifact_count": len(records),
                                "vulnerability_count": len(vulnerability_records),
                                "reason_run_id": reason_run_id,
                            },
                        ),
                    )
                transaction_committed = True
                try:
                    prepared.finish()
                except OSError:
                    logger.exception(
                        "failed to clean Explore run outputs: %s/%s",
                        task_id,
                        intent_id,
                    )

                # 事务成功提交后再触发钉钉推送，确保漏洞 ID 已落库且不会在事务内发生网络 IO 阻塞
                for vuln in vulnerability_records:
                    try:
                        from axiom.modules.vulnerabilities.service import (
                            dispatch_vulnerability_reported_notification,
                        )
                        dispatch_vulnerability_reported_notification(vuln, task_id=task_id)
                    except Exception:
                        logger.exception("Failed to dispatch DingTalk notification for vuln %s", vuln.id)

                result = ExploreCompleted(
                    task_id=task_id,
                    intent_id=intent_id,
                    fact_id=fact_id,
                    reason_run_id=reason_run_id,
                    artifacts=records,
                    vulnerabilities=vulnerability_records,
                )
                try:
                    finalize_explore_state(
                        run_dir,
                        artifacts=records,
                        vulnerabilities=vulnerability_records,
                    )
                except Exception:
                    logger.exception(
                        "failed to finalize Explore internal index: %s/%s",
                        task_id,
                        intent_id,
                    )
                return result
        except BaseException:
            if not transaction_committed:
                prepared.rollback()
            raise

    def report_explore_vulnerability(
        self,
        *,
        task_id: str,
        intent_id: str,
        command: ReportExploreVulnerabilityCommand,
    ) -> ExploreVulnerabilityRecorded:
        run_dir = self.store.run_directory(
            task_id=task_id,
            intent_id=intent_id,
            lease_id=command.lease_id,
        )
        with explore_state_guard(run_dir):
            with self.engine.connect() as conn:
                row = self.repository.input_row(conn, task_id, intent_id)
                self._require_row(row, task_id, intent_id)
                self._require_lease(row, command.lease_id)
                if row["task_status"] != "running":
                    raise LeaseInactiveError("Task is not running")
                if row["kind"] != "explore":
                    raise ConflictError("Intent is not an Explore Intent")
                if row["intent_status"] not in {"running", "concluding"}:
                    raise LeaseInactiveError("Explore Intent lease is not active")
                worker = row["worker"] or "explore"
            report = StoredExploreVulnerability(
                **command.report.model_dump(mode="python"),
                intent_id=intent_id,
                source="explore",
                reported_by=worker,
            )
            after, duplicate = append_explore_vulnerability_locked(run_dir, report)
        return ExploreVulnerabilityRecorded(
            total=len(after.vulnerabilities),
            duplicate=duplicate,
        )

    def _completed(self, conn, task_id: str, intent_id: str, fact_id: str) -> BootstrapCompleted:
        records = self.artifacts.for_intent(conn, task_id, intent_id)
        return BootstrapCompleted(
            task_id=task_id,
            intent_id=intent_id,
            fact_id=fact_id,
            next_reason_run_id=self.repository.reason_run_for_fact(conn, task_id, fact_id)
            or "",
            artifacts=records,
        )

    def _explore_completed(
        self,
        conn,
        task_id: str,
        intent_id: str,
        fact_id: str,
    ) -> ExploreCompleted:
        return ExploreCompleted(
            task_id=task_id,
            intent_id=intent_id,
            fact_id=fact_id,
            reason_run_id=self.repository.reason_run_for_fact(conn, task_id, fact_id),
            artifacts=self.artifacts.for_intent(conn, task_id, intent_id),
            vulnerabilities=[
                item
                for item in self.vulnerabilities.list(conn, task_id)
                if item.intent_id == intent_id
            ],
        )

    @staticmethod
    def _bootstrap_fact(records) -> str:
        labels = {
            "bootstrap.asset_inventory": "资产清单",
            "bootstrap.initial_leads": "初始线索",
            "bootstrap.findings": "已确认问题",
        }
        refs = "；".join(
            f"{labels.get(item.artifact_type, item.artifact_type)} artifact:{item.id}"
            for item in records
        )
        return f"Bootstrap 已完成初始攻击面收集。产物索引：{refs}。"

    @staticmethod
    def _explore_fact(
        *,
        intent_description: str,
        artifacts,
        vulnerability_ids: list[str],
    ) -> str:
        intent_summary = intent_description.strip()
        if not intent_summary.endswith(("。", "！", "？", ".", "!", "?")):
            intent_summary += "。"
        by_type = {item.artifact_type: item for item in artifacts}
        findings = by_type["explore.findings"]
        leads = by_type["explore.leads"]
        evidence_count = sum(item.artifact_type == "explore.evidence" for item in artifacts)
        vulnerability_refs = "、".join(
            f"vulnerability:{item_id}" for item_id in vulnerability_ids
        )
        vulnerability_summary = (
            f"本轮上报漏洞：{vulnerability_refs}。"
            if vulnerability_refs
            else "本轮未上报已确认漏洞。"
        )
        return (
            f"已完成 Explore Intent：{intent_summary}"
            f"已确认事实：artifact:{findings.id}；待验证线索：artifact:{leads.id}；"
            f"证据文件：{evidence_count} 个。{vulnerability_summary}"
        )

    @staticmethod
    def _require_row(row, task_id: str, intent_id: str) -> None:
        if row is None:
            raise NotFoundError(f"Intent not found: {task_id}/{intent_id}")

    @staticmethod
    def _require_lease(row, lease_id: str) -> None:
        if row["lease_id"] != lease_id:
            raise LeaseInactiveError("Intent lease does not match")

    def _credential_project_id(
        self,
        conn,
        task_id: str,
        command: CredentialAccessCommand,
    ) -> str:
        if command.intent_id:
            row = self.repository.input_row(conn, task_id, command.intent_id)
            self._require_row(row, task_id, command.intent_id)
            self._require_lease(row, command.lease_id)
            if row["task_status"] != "running":
                raise LeaseInactiveError("Task is not running")
            if row["intent_status"] not in {"claimed", "running", "concluding"}:
                raise LeaseInactiveError("Intent lease is not active")
            return str(row["project_id"])

        if command.review_run_id:
            run = self.reviews.repository.get(conn, task_id, command.review_run_id)
            if run is None:
                raise NotFoundError(
                    f"Vulnerability ReviewRun not found: {task_id}/{command.review_run_id}"
                )
            if run.lease_id != command.lease_id:
                raise LeaseInactiveError("Vulnerability ReviewRun lease does not match")
            if run.status not in {"claimed", "running", "concluding"}:
                raise LeaseInactiveError("Vulnerability ReviewRun lease is not active")
            task = self.reason.tasks.get(conn, task_id)
            if task is None:
                raise NotFoundError(f"Task not found: {task_id}")
            if task.legacy_readonly:
                raise LeaseInactiveError("Legacy Task is read-only")
            return task.project_id

        reason_run_id = command.reason_run_id or ""
        run = self.reason.repository.get_run(conn, task_id, reason_run_id)
        if run is None:
            raise NotFoundError(f"ReasonRun not found: {task_id}/{reason_run_id}")
        if run.lease_id != command.lease_id:
            raise LeaseInactiveError("ReasonRun lease does not match")
        if run.status not in {"claimed", "running", "concluding"}:
            raise LeaseInactiveError("ReasonRun lease is not active")
        task = self.reason.tasks.get(conn, task_id)
        if task is None:
            raise NotFoundError(f"Task not found: {task_id}")
        if task.status != "running":
            raise LeaseInactiveError("Task is not running")
        return task.project_id
