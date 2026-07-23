from __future__ import annotations

import json

from sqlalchemy import Engine, and_, exists, func, select, update

from axiom.modules.audit.schemas import RecordAuditEvent
from axiom.modules.audit.service import AuditService
from axiom.modules.graph.tables import facts, intent_sources, intents
from axiom.modules.goals.repository import GoalRepository
from axiom.modules.goals.service import GoalService
from axiom.modules.context.tables import context_revisions
from axiom.modules.reason.repository import ReasonRepository
from axiom.modules.reason.policy import ReasonTriggerPolicy
from axiom.modules.reason.schemas import (
    CommitResult,
    DraftIntent,
    DraftTodo,
    DraftTodoUpdate,
    ReasonWorkspace,
    WorkspaceDraft,
)
from axiom.modules.reason.tables import reason_workspaces
from axiom.modules.settings.service import load_runtime_config
from axiom.modules.tasks.repository import TaskRepository
from axiom.modules.tasks.origin import parse_origin, scope_entry_matches_host
from axiom.modules.tasks.tables import tasks
from axiom.modules.todos.repository import TodoRepository
from axiom.modules.todos.schemas import CreateTodo, TodoSource
from axiom.modules.todos.tables import todos
from axiom.modules.vulnerabilities.repository import VulnerabilityRepository
from axiom.shared.clock import parse_utc, utcnow
from axiom.shared.database import immediate_transaction
from axiom.shared.errors import ConflictError, LeaseInactiveError, NotFoundError
from axiom.shared.ids import allocate_id


class ReasonService:
    def __init__(self, engine: Engine, *, trigger_policy: ReasonTriggerPolicy | None = None):
        self.engine = engine
        self.trigger_policy = trigger_policy
        self.repository = ReasonRepository()
        self.tasks = TaskRepository()
        self.todos = TodoRepository()
        self.vulnerabilities = VulnerabilityRepository()
        self.goals = GoalRepository()
        self.audit = AuditService(engine)

    def create_initial(
        self,
        conn,
        *,
        task_id: str,
        source_fact_id: str,
        worker_pool_json: str,
        target: str,
        now: str,
    ) -> str:
        intent_id = self.repository.create_intent(
            conn,
            task_id=task_id,
            source_fact_ids=[source_fact_id],
            worker_pool_json=worker_pool_json,
            target=target,
            description="根据 Bootstrap 产物规划下一轮测试",
            creator="system.bootstrap",
            now=now,
        )
        task = conn.execute(select(tasks).where(tasks.c.id == task_id)).mappings().one()
        self.audit.record_in(
            conn,
            RecordAuditEvent(
                project_id=task["project_id"],
                task_id=task_id,
                intent_id=intent_id,
                event_type="intent.created",
                message="已创建 Reason Intent，等待空闲 Worker 自动领取",
                payload={"phase": "reason", "source_fact_id": source_fact_id},
            ),
        )
        return intent_id

    def advance_after_explore(self, conn, *, task_id: str, now: str) -> str | None:
        active_reason = conn.execute(
            select(intents.c.id)
            .where(
                intents.c.task_id == task_id,
                intents.c.kind == "reason",
                intents.c.status.in_(["pending", "claimed", "running", "concluding"]),
                intents.c.archived.is_(False),
            )
            .order_by(intents.c.created_at, intents.c.id)
            .limit(1)
        ).scalar_one_or_none()
        if active_reason is not None:
            self._set_phase(conn, task_id, "reason", now)
            return active_reason

        reason_source_exists = exists(
            select(intent_sources.c.fact_id)
            .join(
                intents,
                and_(
                    intents.c.task_id == intent_sources.c.task_id,
                    intents.c.id == intent_sources.c.intent_id,
                ),
            )
            .where(
                intent_sources.c.task_id == task_id,
                intent_sources.c.fact_id == facts.c.id,
                intents.c.kind == "reason",
                intents.c.archived.is_(False),
            )
        )
        unplanned = list(
            conn.execute(
                select(facts.c.id, facts.c.created_at)
                .join(
                    intents,
                    and_(
                        intents.c.task_id == facts.c.task_id,
                        intents.c.to_fact_id == facts.c.id,
                    ),
                )
                .where(
                    facts.c.task_id == task_id,
                    facts.c.archived.is_(False),
                    intents.c.kind == "explore",
                    intents.c.status == "completed",
                    ~reason_source_exists,
                )
                .order_by(facts.c.created_at, facts.c.id)
            ).mappings()
        )
        open_explore = conn.execute(
            select(func.count())
            .select_from(intents)
            .where(
                intents.c.task_id == task_id,
                intents.c.kind == "explore",
                intents.c.status.in_(["pending", "claimed", "running", "concluding"]),
                intents.c.archived.is_(False),
            )
        ).scalar_one()
        oldest_age = (
            max(0.0, (parse_utc(now) - parse_utc(unplanned[0]["created_at"])).total_seconds())
            if unplanned
            else 0.0
        )
        config = load_runtime_config(conn)
        policy = self.trigger_policy or ReasonTriggerPolicy(
            batch_size=config.reason_batch_size,
            low_watermark=config.reason_low_watermark,
            max_wait_seconds=config.reason_max_wait_seconds,
        )
        if not policy.should_trigger(
            unplanned_facts=len(unplanned),
            open_explore_intents=open_explore,
            oldest_fact_age_seconds=oldest_age,
        ):
            self._set_phase(conn, task_id, "explore" if open_explore else "waiting", now)
            return None

        task = conn.execute(select(tasks).where(tasks.c.id == task_id)).mappings().one()
        reason_id = self.repository.create_intent(
            conn,
            task_id=task_id,
            source_fact_ids=[row["id"] for row in unplanned],
            worker_pool_json=task["worker_pool_json"],
            target=task["origin_host"],
            description=f"根据 {len(unplanned)} 个新 Fact 更新 Todo 并规划下一轮测试",
            creator="system.explore",
            now=now,
        )
        self.audit.record_in(
            conn,
            RecordAuditEvent(
                project_id=task["project_id"],
                task_id=task_id,
                intent_id=reason_id,
                event_type="intent.created",
                message="发现新的未规划 Fact，已创建 Reason Intent",
                payload={"phase": "reason", "unplanned_fact_count": len(unplanned)},
            ),
        )
        self._set_phase(conn, task_id, "reason", now)
        return reason_id

    @staticmethod
    def _set_phase(conn, task_id: str, phase: str, now: str) -> None:
        conn.execute(
            update(tasks)
            .where(tasks.c.id == task_id, tasks.c.status == "running")
            .values(phase=phase, updated_at=now)
        )

    def open(
        self,
        *,
        task_id: str,
        intent_id: str,
        lease_id: str,
        context_revision: int,
    ) -> ReasonWorkspace:
        with immediate_transaction(self.engine) as conn:
            row = self._reason_intent(conn, task_id, intent_id)
            self._require_lease(row, lease_id)
            if row["status"] not in {"running", "concluding"}:
                raise ConflictError("Reason Intent is not active")
            context_exists = conn.execute(
                select(context_revisions.c.revision).where(
                    context_revisions.c.task_id == task_id,
                    context_revisions.c.revision == context_revision,
                    context_revisions.c.intent_id == intent_id,
                    context_revisions.c.lease_id == lease_id,
                    context_revisions.c.phase == "reason",
                )
            ).scalar_one_or_none()
            if context_exists is None:
                raise ConflictError("Reason Context does not match this lease")
            existing = self.repository.for_attempt(conn, task_id, intent_id, lease_id)
            if existing is not None:
                if existing.status == "open" and existing.worker == (row["worker"] or ""):
                    return existing
                raise ConflictError("Reason attempt already has a closed Workspace")
            workspace = self.repository.open(
                conn,
                task_id=task_id,
                intent_id=intent_id,
                lease_id=lease_id,
                worker=row["worker"] or "",
                context_revision=context_revision,
                now=utcnow(),
            )
            self.audit.record_in(
                conn,
                RecordAuditEvent(
                    task_id=task_id,
                    intent_id=intent_id,
                    worker=row["worker"] or "",
                    event_type="reason.workspace_opened",
                    message="Reason 已打开规划工作区",
                    payload={"phase": "reason", "workspace_id": workspace.id},
                ),
            )
            return workspace

    def get(self, workspace_id: str) -> ReasonWorkspace:
        with self.engine.connect() as conn:
            workspace = self.repository.get(conn, workspace_id)
        if workspace is None:
            raise NotFoundError(f"Reason Workspace not found: {workspace_id}")
        return workspace

    def abandon(self, workspace_id: str, *, lease_id: str, failure_reason: str) -> ReasonWorkspace:
        with immediate_transaction(self.engine) as conn:
            workspace = self._require_open(conn, workspace_id)
            self._verify_workspace_lease(conn, workspace, lease_id)
            abandoned = self.repository.abandon(
                conn,
                workspace_id,
                failure_reason=failure_reason.strip() or "Reason execution failed",
                now=utcnow(),
            )
            self.audit.record_in(
                conn,
                RecordAuditEvent(
                    task_id=workspace.task_id,
                    intent_id=workspace.intent_id,
                    worker=workspace.worker,
                    event_type="reason.workspace_abandoned",
                    level="warning",
                    message=f"Reason 规划未提交：{failure_reason.strip() or '执行失败'}",
                    payload={"phase": "reason", "workspace_id": workspace_id, "failure_reason": failure_reason},
                ),
            )
            return abandoned

    def add_todo(self, workspace_id: str, todo: DraftTodo) -> ReasonWorkspace:
        with immediate_transaction(self.engine) as conn:
            workspace = self._require_open(conn, workspace_id)
            self._validate_sources(conn, workspace.task_id, todo.sources)
            if any(item.ref == todo.ref for item in workspace.draft.todo_adds):
                raise ConflictError(f"Todo draft ref already exists: {todo.ref}")
            draft = workspace.draft.model_copy(update={"todo_adds": [*workspace.draft.todo_adds, todo]})
            return self.repository.save_draft(conn, workspace_id, draft, now=utcnow())

    def update_todo(self, workspace_id: str, update_command: DraftTodoUpdate) -> ReasonWorkspace:
        with immediate_transaction(self.engine) as conn:
            workspace = self._require_open(conn, workspace_id)
            self._validate_sources(conn, workspace.task_id, update_command.source_adds)
            if self.todos.get(conn, workspace.task_id, update_command.todo_id) is None:
                raise NotFoundError(f"Todo not found: {update_command.todo_id}")
            updates = [
                item for item in workspace.draft.todo_updates if item.todo_id != update_command.todo_id
            ]
            previous = next(
                (item for item in workspace.draft.todo_updates if item.todo_id == update_command.todo_id),
                None,
            )
            if previous is not None:
                merged = previous.model_dump()
                merged.update(update_command.model_dump(exclude_unset=True))
                update_command = DraftTodoUpdate.model_validate(merged)
            updates.append(update_command)
            draft = workspace.draft.model_copy(update={"todo_updates": updates})
            return self.repository.save_draft(conn, workspace_id, draft, now=utcnow())

    def propose_intent(self, workspace_id: str, proposal: DraftIntent) -> ReasonWorkspace:
        with immediate_transaction(self.engine) as conn:
            workspace = self._require_open(conn, workspace_id)
            self._validate_intent(conn, workspace.task_id, workspace.draft, proposal)
            identity = self._intent_identity(proposal)
            if any(self._intent_identity(item) == identity for item in workspace.draft.intent_proposals):
                raise ConflictError("Duplicate Intent proposal")
            draft = workspace.draft.model_copy(
                update={"intent_proposals": [*workspace.draft.intent_proposals, proposal]}
            )
            return self.repository.save_draft(conn, workspace_id, draft, now=utcnow())

    def submit(self, workspace_id: str, draft: WorkspaceDraft, *, lease_id: str) -> CommitResult:
        with immediate_transaction(self.engine) as conn:
            workspace = self.repository.get(conn, workspace_id)
            if workspace is None:
                raise NotFoundError(f"Reason Workspace not found: {workspace_id}")
            self._verify_workspace_lease(conn, workspace, lease_id)
            if workspace.status == "committed":
                todo_ids, intent_ids, vulnerability_ids = self.repository.commit_result(
                    conn,
                    workspace,
                )
                return CommitResult(
                    workspace=workspace,
                    created_todo_ids=todo_ids,
                    created_intent_ids=intent_ids,
                    created_vulnerability_ids=vulnerability_ids,
                )
            if workspace.status != "open":
                raise ConflictError("Reason Workspace is not open")
            reason_intent = self._reason_intent(conn, workspace.task_id, workspace.intent_id)
            if reason_intent["status"] not in {"running", "concluding"}:
                raise LeaseInactiveError("Reason Intent is not active")
            task = self.tasks.get(conn, workspace.task_id)
            if task is None:
                raise NotFoundError(f"Task not found: {workspace.task_id}")
            if task.status != "running":
                raise LeaseInactiveError("Task is not writable")
            if task.legacy_readonly:
                raise ConflictError("Task is not writable")
            for item in draft.todo_adds:
                self._validate_sources(conn, workspace.task_id, item.sources)
            refs = [item.ref for item in draft.todo_adds]
            if len(refs) != len(set(refs)):
                raise ConflictError("Duplicate Todo draft ref")
            for item in draft.todo_updates:
                if item.todo_id in refs:
                    self._validate_sources(conn, workspace.task_id, item.source_adds)
                    continue
                if self.todos.get(conn, workspace.task_id, item.todo_id) is None:
                    raise NotFoundError(f"Todo not found: {item.todo_id}")
                self._validate_sources(conn, workspace.task_id, item.source_adds)
            for proposal in draft.intent_proposals:
                self._validate_intent(conn, workspace.task_id, draft, proposal)
            assessment = draft.goal_assessment
            if assessment is not None:
                if assessment.achieved and draft.intent_proposals:
                    raise ConflictError("A completed Goal cannot dispatch new Intents")
            existing_open_explore = conn.execute(
                select(func.count())
                .select_from(intents)
                .where(
                    intents.c.task_id == workspace.task_id,
                    intents.c.kind == "explore",
                    intents.c.status.in_(["pending", "claimed", "running", "concluding"]),
                    intents.c.archived.is_(False),
                )
            ).scalar_one()
            if not (assessment and assessment.achieved) and not draft.intent_proposals and not existing_open_explore:
                raise ConflictError(
                    "Reason must dispatch actionable work or provide an evidence-backed achieved Goal"
                )

            now = utcnow()
            todo_refs: dict[str, str] = {}
            created_todo_ids: list[str] = []
            for todo in draft.todo_adds:
                staged_update = next(
                    (item for item in draft.todo_updates if item.todo_id == todo.ref),
                    None,
                )
                sources = list(todo.sources)
                if staged_update is not None:
                    sources.extend(staged_update.source_adds)
                unique_sources = {
                    (source.type, source.id): source
                    for source in sources
                }
                created = self.todos.create(
                    conn,
                    task_id=workspace.task_id,
                    command=CreateTodo(
                        title=(staged_update.title if staged_update and staged_update.title else todo.title),
                        category=(
                            staged_update.category
                            if staged_update and staged_update.category
                            else todo.category
                        ),
                        theme=(
                            staged_update.theme
                            if staged_update and staged_update.theme is not None
                            else todo.theme
                        ),
                        priority=(
                            staged_update.priority
                            if staged_update and staged_update.priority is not None
                            else todo.priority
                        ),
                        status=(
                            staged_update.status
                            if staged_update and staged_update.status is not None
                            else todo.status
                        ),
                        status_reason=(
                            staged_update.status_reason
                            if staged_update and staged_update.status_reason is not None
                            else todo.status_reason
                        ),
                        sources=list(unique_sources.values()),
                        actor=workspace.worker,
                    ),
                    now=now,
                )
                todo_refs[todo.ref] = created.id
                created_todo_ids.append(created.id)

            if assessment is not None:
                evidence_refs = [
                    f"todo:{todo_refs[item_id]}"
                    if kind == "todo" and item_id in todo_refs
                    else ref
                    for ref in assessment.evidence_refs
                    for kind, separator, item_id in [ref.partition(":")]
                ]
                assessment = assessment.model_copy(
                    update={"evidence_refs": list(dict.fromkeys(evidence_refs))}
                )
                GoalService.validate_evidence(
                    conn,
                    workspace.task_id,
                    assessment.evidence_refs,
                    achieved=assessment.achieved,
                )

            for update_command in draft.todo_updates:
                if update_command.todo_id in todo_refs:
                    continue
                values = update_command.model_dump(exclude_none=True, exclude={"todo_id", "source_adds"})
                if update_command.status == "completed":
                    values["completed_at"] = now
                elif update_command.status is not None:
                    values["completed_at"] = None
                values["updated_at"] = now
                self.todos.update(conn, workspace.task_id, update_command.todo_id, values)
                self.todos.add_sources(
                    conn,
                    task_id=workspace.task_id,
                    todo_id=update_command.todo_id,
                    sources=update_command.source_adds,
                )

            created_intent_ids: list[str] = []
            for proposal in draft.intent_proposals:
                todo_id = proposal.todo_id or todo_refs.get(proposal.todo_ref or "")
                if todo_id is None:
                    raise ConflictError(f"Intent references unknown Todo: {proposal.todo_ref}")
                intent_id = allocate_id(conn, scope=workspace.task_id, kind="intent", prefix="i")
                conn.execute(
                    intents.insert().values(
                        id=intent_id,
                        task_id=workspace.task_id,
                        todo_id=todo_id,
                        kind="explore",
                        description=proposal.description,
                        status="pending",
                        creator=workspace.worker,
                        worker=None,
                        worker_pool_json=json.dumps(task.worker_pool, ensure_ascii=False),
                        targets_json=json.dumps(proposal.targets, ensure_ascii=False),
                        execute_timeout_seconds=proposal.execute_seconds,
                        conclude_timeout_seconds=proposal.conclude_seconds,
                        retry_count=0,
                        infrastructure_retry_count=0,
                        last_failure_kind="",
                        failure_class="",
                        created_at=now,
                        archived=False,
                    )
                )
                conn.execute(
                    intent_sources.insert(),
                    [
                        {
                            "task_id": workspace.task_id,
                            "intent_id": intent_id,
                            "fact_id": fact_id,
                        }
                        for fact_id in proposal.from_facts
                    ],
                )
                conn.execute(
                    update(todos)
                    .where(
                        todos.c.task_id == workspace.task_id,
                        todos.c.id == todo_id,
                        todos.c.status == "pending",
                    )
                    .values(status="active", updated_at=now)
                )
                created_intent_ids.append(intent_id)

            created_vulnerability_ids: list[str] = []
            for report in draft.vulnerability_reports:
                report = report.model_copy(
                    update={
                        "intent_id": workspace.intent_id,
                        "reported_by": workspace.worker,
                    }
                )
                vulnerability = self.vulnerabilities.create(
                    conn,
                    task_id=workspace.task_id,
                    body=report,
                    now=now,
                )
                created_vulnerability_ids.append(vulnerability.id)

            completed = conn.execute(
                update(intents)
                .where(
                    intents.c.task_id == workspace.task_id,
                    intents.c.id == workspace.intent_id,
                    intents.c.lease_id == lease_id,
                    intents.c.status.in_(["running", "concluding"]),
                )
                .values(
                    status="completed",
                    concluded_at=now,
                    last_heartbeat_at=now,
                    next_retry_at=None,
                    last_failure_kind="",
                    failure_class="",
                    activity_kind="completed",
                    activity_summary="Reason 已完成规划并提交",
                    activity_tool="",
                    activity_updated_at=now,
                )
            )
            if not completed.rowcount:
                raise ConflictError("Reason completion lost a concurrent race")
            if assessment is not None:
                self.goals.create(
                    conn,
                    task_id=workspace.task_id,
                    assessment=assessment,
                    evaluator=workspace.worker,
                    intent_id=workspace.intent_id,
                    workspace_id=workspace.id,
                    now=now,
                )
            if assessment and assessment.achieved:
                GoalService.complete_task(
                    conn,
                    workspace.task_id,
                    now=now,
                    exclude_intent_id=workspace.intent_id,
                )
            else:
                conn.execute(
                    update(tasks)
                    .where(tasks.c.id == workspace.task_id)
                    .values(phase="explore", updated_at=now)
                )
            conn.execute(
                update(reason_workspaces)
                .where(reason_workspaces.c.id == workspace_id)
                .values(draft_json=draft.model_dump_json(), updated_at=now)
            )
            committed = self.repository.mark_committed(
                conn,
                workspace_id,
                created_todo_ids=created_todo_ids,
                created_intent_ids=created_intent_ids,
                created_vulnerability_ids=created_vulnerability_ids,
                now=now,
            )
            self.audit.record_in(
                conn,
                RecordAuditEvent(
                    project_id=task.project_id,
                    task_id=workspace.task_id,
                    intent_id=workspace.intent_id,
                    workspace_id=workspace.id,
                    worker=workspace.worker,
                    event_type="reason.committed",
                    message=f"Reason 已提交规划：新增 {len(created_intent_ids)} 个 Intent、{len(created_todo_ids)} 个 Todo",
                    payload={
                        "phase": "reason",
                        "created_todo_ids": created_todo_ids,
                        "created_intent_ids": created_intent_ids,
                        "created_vulnerability_ids": created_vulnerability_ids,
                        "goal_achieved": bool(assessment and assessment.achieved),
                    },
                ),
            )
            return CommitResult(
                workspace=committed,
                created_todo_ids=created_todo_ids,
                created_intent_ids=created_intent_ids,
                created_vulnerability_ids=created_vulnerability_ids,
            )

    def commit(self, workspace_id: str, *, lease_id: str) -> CommitResult:
        workspace = self.get(workspace_id)
        return self.submit(workspace_id, workspace.draft, lease_id=lease_id)

    def _require_open(self, conn, workspace_id: str) -> ReasonWorkspace:
        workspace = self.repository.get(conn, workspace_id)
        if workspace is None:
            raise NotFoundError(f"Reason Workspace not found: {workspace_id}")
        if workspace.status != "open":
            raise ConflictError("Reason Workspace is not open")
        return workspace

    def _reason_intent(self, conn, task_id: str, intent_id: str):
        row = conn.execute(
            select(intents).where(
                intents.c.task_id == task_id,
                intents.c.id == intent_id,
                intents.c.kind == "reason",
            )
        ).mappings().one_or_none()
        if row is None:
            raise NotFoundError(f"Reason Intent not found: {task_id}/{intent_id}")
        return row

    @staticmethod
    def _require_lease(row, lease_id: str) -> None:
        if row["lease_id"] != lease_id:
            raise LeaseInactiveError("Reason Intent lease does not match")

    def _verify_workspace_lease(self, conn, workspace: ReasonWorkspace, lease_id: str) -> None:
        if workspace.lease_id != lease_id:
            raise LeaseInactiveError("Reason Workspace lease does not match")
        row = self._reason_intent(conn, workspace.task_id, workspace.intent_id)
        self._require_lease(row, lease_id)

    def _validate_sources(self, conn, task_id: str, sources: list[TodoSource]) -> None:
        for source in sources:
            if not self.todos.source_exists(conn, task_id=task_id, source=source):
                raise ConflictError(f"Todo source not found: {source.type}:{source.id}")

    def _validate_intent(self, conn, task_id: str, draft: WorkspaceDraft, proposal: DraftIntent) -> None:
        if proposal.todo_ref:
            if not any(item.ref == proposal.todo_ref for item in draft.todo_adds):
                raise ConflictError(f"Intent references unknown Todo draft: {proposal.todo_ref}")
        if proposal.todo_id and self.todos.get(conn, task_id, proposal.todo_id) is None:
            raise NotFoundError(f"Todo not found: {proposal.todo_id}")
        for fact_id in proposal.from_facts:
            exists = conn.execute(
                select(facts.c.id).where(
                    facts.c.task_id == task_id,
                    facts.c.id == fact_id,
                    facts.c.archived.is_(False),
                )
            ).scalar_one_or_none()
            if exists is None:
                raise ConflictError(f"Intent source Fact not found: {fact_id}")
        task = self.tasks.get(conn, task_id)
        assert task is not None
        for target in proposal.targets:
            if not self._target_allowed(task, target):
                raise ConflictError(f"Intent target outside Task Scope: {target}")

    @staticmethod
    def _intent_identity(proposal: DraftIntent) -> tuple:
        return (
            proposal.todo_ref or proposal.todo_id,
            proposal.description,
            tuple(proposal.targets),
        )

    @staticmethod
    def _target_allowed(task, target: str) -> bool:
        scope = task.scope
        if not scope.enabled:
            return True
        try:
            candidate = parse_origin(target)
        except Exception:
            return False
        host = candidate.host
        if any(scope_entry_matches_host(item, host) for item in scope.exclude):
            return False
        if not scope.targets:
            return False
        if not any(scope_entry_matches_host(item, host) for item in scope.targets):
            return False

        # An explicit port in the Task origin is an authorization boundary for
        # that host.  Reason may express the target as an authority or URL, but
        # it must preserve the exact port instead of silently widening scope.
        if host == task.origin.host and task.origin.port is not None:
            return candidate.port == task.origin.port
        return True
