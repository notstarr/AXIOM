from __future__ import annotations

import json

from datetime import timedelta

from sqlalchemy import Engine, update

from axiom.modules.artifacts.repository import ArtifactRepository
from axiom.modules.audit.schemas import RecordAuditEvent
from axiom.modules.audit.service import AuditService
from axiom.modules.context.service import ContextService
from axiom.modules.credentials.repository import CredentialRepository
from axiom.modules.execution.repository import ExecutionRepository
from axiom.modules.execution.schemas import (
    ActivityCommand,
    BootstrapCompleted,
    CompleteBootstrapCommand,
    CompleteExploreCommand,
    ExecutionInput,
    ExploreCompleted,
    LeaseCommand,
    ReleaseIntentCommand,
    ReleasedIntent,
    StartedIntent,
)
from axiom.modules.graph.tables import facts
from axiom.modules.reason.service import ReasonService
from axiom.modules.settings.service import load_runtime_config
from axiom.modules.workers.tables import workers
from axiom.modules.skills.service import SkillService
from axiom.modules.vulnerabilities.repository import VulnerabilityRepository
from axiom.shared.clock import format_utc, parse_utc, utcnow
from axiom.shared.database import immediate_transaction
from axiom.shared.errors import ConflictError, LeaseInactiveError, NotFoundError
from axiom.shared.ids import allocate_id
from axiom.shared.storage import FileStore


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
            # Bootstrap and Reason are created without per-Intent budgets.  The
            # effective budget must therefore be resolved here, at the single
            # boundary where execution input is built.  Previously the NULL
            # values passed through to Pi, which silently selected its legacy
            # fallbacks (900s Bootstrap execute / 180s conclude), ignoring the
            # values configured in the UI and persisted in RuntimeConfig.
            runtime = load_runtime_config(conn)
            execute_timeout = row["execute_timeout_seconds"]
            conclude_timeout = row["conclude_timeout_seconds"]
            if row["kind"] == "bootstrap":
                execute_timeout = execute_timeout or runtime.bootstrap_execute_seconds
                conclude_timeout = conclude_timeout or runtime.bootstrap_conclude_seconds
            elif row["kind"] == "reason":
                execute_timeout = execute_timeout or runtime.reason_execute_seconds
            values = {
                "worker_name": row["worker"] or "",
                "intent_kind": row["kind"],
                "intent_description": row["intent_description"],
                "intent_targets": json.loads(row["targets_json"] or "[]"),
                "intent_source_facts": self.repository.source_facts(conn, task_id, intent_id),
                "todo_id": row["todo_id"],
                "credentials": self.credentials.for_project_materials(conn, row["project_id"]),
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
            credentials=values["credentials"],
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

        prepared = self.store.prepare_text_files(
            task_id=task_id,
            intent_id=intent_id,
            files=command.artifacts.files(),
        )
        now = utcnow()
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
                fact_id = allocate_id(conn, scope=task_id, kind="fact", prefix="f")
                conn.execute(
                    facts.insert().values(
                        id=fact_id,
                        task_id=task_id,
                        description=command.summary,
                        created_at=now,
                        archived=False,
                    )
                )
                records = self.artifacts.create_bootstrap_bundle(
                    conn,
                    task_id=task_id,
                    intent_id=intent_id,
                    prepared=prepared.files,
                    created_by=row["worker"] or "bootstrap",
                    now=now,
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
                next_intent_id = self.reason.create_initial(
                    conn,
                    task_id=task_id,
                    source_fact_id=fact_id,
                    worker_pool_json=row["worker_pool_json"],
                    target=row["origin_host"],
                    now=now,
                )
                self.repository.complete_task_phase(conn, task_id, now)
            prepared.finish()
            return BootstrapCompleted(
                task_id=task_id,
                intent_id=intent_id,
                fact_id=fact_id,
                next_intent_id=next_intent_id,
                artifacts=records,
            )
        except BaseException:
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

        prepared = self.store.prepare_text_files(
            task_id=task_id,
            intent_id=intent_id,
            files={item.filename: item.content for item in command.output.artifacts},
        )
        specs = {
            item.filename: (item.artifact_type, item.summary)
            for item in command.output.artifacts
        }
        now = utcnow()
        try:
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
                fact_id = allocate_id(conn, scope=task_id, kind="fact", prefix="f")
                conn.execute(
                    facts.insert().values(
                        id=fact_id,
                        task_id=task_id,
                        description=command.output.fact,
                        created_at=now,
                        archived=False,
                    )
                )
                records = self.artifacts.create_explore_bundle(
                    conn,
                    task_id=task_id,
                    intent_id=intent_id,
                    prepared=prepared.files,
                    specs=specs,
                    created_by=row["worker"] or "explore",
                    now=now,
                )
                vulnerability_records = []
                for report in command.output.vulnerabilities:
                    report = report.model_copy(
                        update={
                            "intent_id": intent_id,
                            "reported_by": row["worker"] or report.reported_by,
                        }
                    )
                    vulnerability_records.append(
                        self.vulnerabilities.create(
                            conn,
                            task_id=task_id,
                            body=report,
                            now=now,
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
                reason_intent_id = self.reason.advance_after_explore(
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
                            "reason_intent_id": reason_intent_id,
                        },
                    ),
                )
            prepared.finish()
            return ExploreCompleted(
                task_id=task_id,
                intent_id=intent_id,
                fact_id=fact_id,
                reason_intent_id=reason_intent_id,
                artifacts=records,
                vulnerabilities=vulnerability_records,
            )
        except BaseException:
            prepared.rollback()
            raise

    def _completed(self, conn, task_id: str, intent_id: str, fact_id: str) -> BootstrapCompleted:
        records = self.artifacts.for_intent(conn, task_id, intent_id)
        return BootstrapCompleted(
            task_id=task_id,
            intent_id=intent_id,
            fact_id=fact_id,
            next_intent_id=self.repository.reason_intent_for_fact(conn, task_id, fact_id)
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
            reason_intent_id=self.repository.reason_intent_for_fact(conn, task_id, fact_id),
            artifacts=self.artifacts.for_intent(conn, task_id, intent_id),
            vulnerabilities=[
                item
                for item in self.vulnerabilities.list(conn, task_id)
                if item.intent_id == intent_id
            ],
        )

    @staticmethod
    def _require_row(row, task_id: str, intent_id: str) -> None:
        if row is None:
            raise NotFoundError(f"Intent not found: {task_id}/{intent_id}")

    @staticmethod
    def _require_lease(row, lease_id: str) -> None:
        if row["lease_id"] != lease_id:
            raise LeaseInactiveError("Intent lease does not match")
