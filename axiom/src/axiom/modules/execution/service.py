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
    LeaseCommand,
    ReportExploreVulnerabilityCommand,
    ReleaseIntentCommand,
    ReleasedIntent,
    StartedIntent,
)
from axiom.modules.execution.stage_outputs import (
    EXPLORE_FINDINGS_FILE,
    StageOutputError,
    append_explore_vulnerability_locked,
    bootstrap_files,
    explore_state_guard,
    explore_files,
    finalize_explore_state,
    load_explore_state,
    write_bootstrap_index,
)
from axiom.modules.graph.tables import facts
from axiom.modules.reason.service import ReasonService
from axiom.modules.settings.service import load_runtime_config
from axiom.modules.workers.tables import workers
from axiom.modules.skills.service import SkillService
from axiom.modules.vulnerabilities.repository import VulnerabilityRepository
from axiom.modules.vulnerabilities.schemas import StoredExploreVulnerability, VulnerabilityReport
from axiom.shared.clock import format_utc, parse_utc, utcnow
from axiom.shared.database import immediate_transaction
from axiom.shared.errors import ConflictError, LeaseInactiveError, NotFoundError
from axiom.shared.ids import allocate_id
from axiom.shared.storage import FileStore


logger = logging.getLogger(__name__)


class ExecutionService:
    def __init__(
        self,
        engine: Engine,
        store: FileStore,
        context: ContextService,
        reason: ReasonService,
        skills: SkillService,
    ):
        self.engine = engine
        self.store = store
        self.repository = ExecutionRepository()
        self.artifacts = ArtifactRepository()
        self.context = context
        self.reason = reason
        self.skills = skills
        self.credentials = CredentialRepository()
        self.vulnerabilities = VulnerabilityRepository()
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
            skill_directories=self.skills.active_paths(),
            guard_enabled=bool(row["guard_enabled"]),
            scope=values["scope"],
            run_dir=str(
                self.store.run_directory(
                    task_id=task_id,
                    intent_id=intent_id,
                    lease_id=command.lease_id,
                )
            ),
            execute_timeout_seconds=values["execute_timeout_seconds"],
            conclude_timeout_seconds=values["conclude_timeout_seconds"],
            context_revision=context.revision,
            context_manifest_path=context.manifest_path,
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

        context = self.context.build(
            task_id=task_id,
            phase="reason",
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
        return ExecutionInput(
            task_id=task_id,
            project_id=task.project_id,
            intent_id=None,
            reason_run_id=reason_run_id,
            lease_id=command.lease_id,
            worker_name=run.worker or "",
            intent_kind="reason",
            intent_description=trigger_summary,
            intent_targets=list(task.scope.targets) or [task.origin.host],
            intent_source_facts=run.source_fact_ids,
            todo_id=None,
            origin=task.origin.original_input,
            goal=task.goal,
            hints=hints,
            skill_directories=self.skills.active_paths(),
            guard_enabled=task.guard_enabled,
            scope=task.scope.model_dump(mode="json"),
            run_dir=str(
                self.store.run_directory(
                    task_id=task_id,
                    reason_run_id=reason_run_id,
                    lease_id=command.lease_id,
                )
            ),
            execute_timeout_seconds=run.execute_timeout_seconds,
            conclude_timeout_seconds=run.conclude_timeout_seconds,
            context_revision=context.revision,
            context_manifest_path=context.manifest_path,
        )

    def start(self, *, task_id: str, intent_id: str, command: LeaseCommand) -> StartedIntent:
        now = utcnow()
        with immediate_transaction(self.engine) as conn:
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
        with immediate_transaction(self.engine) as conn:
            if not self.repository.heartbeat(
                conn,
                task_id=task_id,
                intent_id=intent_id,
                lease_id=command.lease_id,
                now=utcnow(),
            ):
                raise LeaseInactiveError("Intent lease is no longer active")

    def activity(self, *, task_id: str, intent_id: str, command: ActivityCommand) -> None:
        with immediate_transaction(self.engine) as conn:
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

    def begin_conclude(self, *, task_id: str, intent_id: str, command: LeaseCommand) -> None:
        with immediate_transaction(self.engine) as conn:
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
        with immediate_transaction(self.engine) as conn:
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
            with immediate_transaction(self.engine) as conn:
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
                self.repository.complete_task_phase(conn, task_id, now)
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
                for report in state.vulnerabilities:
                    missing = [
                        path for path in report.evidence_paths if path not in available_outputs
                    ]
                    if missing:
                        raise StageOutputError(
                            "漏洞证据文件未就绪：" + "、".join(missing)
                        )
                with immediate_transaction(self.engine) as conn:
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
                        evidence_artifact_ids = [
                            artifact_by_relative[path].id for path in report.evidence_paths
                        ]
                        vulnerability_records.append(
                            self.vulnerabilities.create(
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
