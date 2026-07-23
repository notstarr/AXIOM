from __future__ import annotations

import json
import time

from sqlalchemy import Engine, func, select, update
from sqlalchemy.exc import OperationalError

from axiom.infrastructure.database.metadata import (
    artifacts,
    facts,
    intent_sources,
    intents,
    reason_workspaces,
    tasks,
    todos,
    vulnerabilities,
)
from axiom.modules.audit.schemas import RecordAuditEvent
from axiom.modules.audit.service import AuditService
from axiom.modules.common.ids import allocate_readable_id
from axiom.modules.common.time import utcnow
from axiom.modules.reason_workspace.repository import (
    ReasonWorkspaceRepository,
    WorkspaceDraftConflict,
)
from axiom.modules.reason_workspace.schemas import (
    CommitResult,
    DraftIntent,
    DraftTodo,
    DraftTodoUpdate,
    ReasonWorkspace,
    WorkspaceDraft,
)
from axiom.modules.tasks.repository import TaskRepository
from axiom.modules.tasks.scope import normalize_intent_target, target_allowed
from axiom.modules.todos.repository import TodoRepository
from axiom.modules.todos.schemas import CreateTodo, TodoSource


class ReasonWorkspaceService:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.repository = ReasonWorkspaceRepository()
        self.task_repository = TaskRepository()
        self.todo_repository = TodoRepository()
        self.audit = AuditService(engine)

    def open(self, task_id: str, *, worker: str, context_revision: int) -> ReasonWorkspace:
        worker = worker.strip()
        if not worker:
            raise ValueError("worker must not be empty")
        with self.engine.begin() as conn:
            task = self.task_repository.get(conn, task_id)
            if task is None:
                raise ValueError(f"Task not found: {task_id}")
            if task.legacy_readonly:
                raise ValueError("Legacy task is read-only")
            if task.status != "running":
                raise ValueError("Task is not running")
            if task.reason_worker != worker:
                if task.reason_worker:
                    raise ValueError(f"Task Reason is currently claimed by {task.reason_worker}")
                raise ValueError("Task Reason is not currently claimed")
            workspace = self.repository.open(
                conn,
                task_id=task_id,
                worker=worker,
                context_revision=context_revision,
                now=utcnow(),
            )
            self.audit.record_in(
                conn,
                RecordAuditEvent(
                    project_id=task.project_id,
                    task_id=task_id,
                    workspace_id=workspace.id,
                    worker=worker,
                    event_type="reason_workspace.opened",
                    message=f"Reason Workspace {workspace.id} opened",
                ),
            )
            return workspace

    def get(self, workspace_id: str) -> ReasonWorkspace:
        with self.engine.connect() as conn:
            workspace = self.repository.get(conn, workspace_id)
        if workspace is None:
            raise ValueError(f"Reason Workspace not found: {workspace_id}")
        return workspace

    def abandon(self, workspace_id: str, *, worker: str, failure_reason: str) -> ReasonWorkspace:
        with self.engine.begin() as conn:
            workspace = self._require_open(conn, workspace_id)
            if workspace.worker != worker:
                raise ValueError(f"Reason Workspace is owned by {workspace.worker}")
            reason = failure_reason.strip() or "Reason execution failed"
            abandoned = self.repository.abandon(
                conn,
                workspace_id,
                failure_reason=reason,
                now=utcnow(),
            )
            task = self.task_repository.get(conn, workspace.task_id)
            self.audit.record_in(
                conn,
                RecordAuditEvent(
                    project_id=task.project_id if task else None,
                    task_id=workspace.task_id,
                    workspace_id=workspace_id,
                    worker=worker,
                    event_type="reason_workspace.abandoned",
                    level="warning",
                    message=f"Reason Workspace {workspace_id} abandoned",
                    payload={"failure_reason": reason},
                ),
            )
            return abandoned

    def add_todo(self, workspace_id: str, todo: DraftTodo) -> ReasonWorkspace:
        def mutate(conn, workspace: ReasonWorkspace) -> None:
            task = self.task_repository.get(conn, workspace.task_id)
            if task is None:
                raise ValueError(f"Task not found: {workspace.task_id}")
            if any(item.ref == todo.ref for item in workspace.draft.todo_adds):
                raise ValueError(f"Todo draft ref already exists: {todo.ref}")
            normalized = self._augment_sources(
                conn,
                workspace.task_id,
                todo.sources,
                required_bootstrap_fact=self._initial_bootstrap_fact(conn, workspace.task_id),
            )
            for source in normalized:
                if not self.todo_repository.source_exists(
                    conn,
                    task_id=workspace.task_id,
                    project_id=task.project_id,
                    source=source,
                ):
                    raise ValueError(f"Todo source not found: {source.type}:{source.id}")
            workspace.draft.todo_adds.append(todo.model_copy(update={"sources": normalized}))

        return self._mutate_draft(workspace_id, mutate)

    def update_todo(self, workspace_id: str, todo: DraftTodoUpdate) -> ReasonWorkspace:
        def mutate(conn, workspace: ReasonWorkspace) -> None:
            task = self.task_repository.get(conn, workspace.task_id)
            if task is None:
                raise ValueError(f"Task not found: {workspace.task_id}")
            existing = self.todo_repository.get(conn, workspace.task_id, todo.todo_id)
            if existing is None:
                raise ValueError(f"Todo not found: {todo.todo_id}")
            normalized = self._augment_sources(
                conn,
                workspace.task_id,
                todo.source_adds,
            )
            for source in normalized:
                if not self.todo_repository.source_exists(
                    conn,
                    task_id=workspace.task_id,
                    project_id=task.project_id,
                    source=source,
                ):
                    raise ValueError(f"Todo source not found: {source.type}:{source.id}")
            previous = next(
                (
                    item
                    for item in workspace.draft.todo_updates
                    if item.todo_id == todo.todo_id
                ),
                None,
            )
            if previous is None:
                normalized_todo = todo.model_copy(update={"source_adds": normalized})
            else:
                merged = previous.model_dump()
                merged.update(todo.model_dump(exclude_unset=True, exclude={"source_adds"}))
                merged["source_adds"] = [*previous.source_adds, *normalized]
                normalized_todo = DraftTodoUpdate.model_validate(merged)
            workspace.draft.todo_updates = [
                item for item in workspace.draft.todo_updates if item.todo_id != todo.todo_id
            ]
            workspace.draft.todo_updates.append(normalized_todo)

        return self._mutate_draft(workspace_id, mutate)

    def propose_intent(self, workspace_id: str, intent: DraftIntent) -> ReasonWorkspace:
        def mutate(conn, workspace: ReasonWorkspace) -> None:
            task = self.task_repository.get(conn, workspace.task_id)
            if task is None:
                raise ValueError(f"Task not found: {workspace.task_id}")
            self._validate_intent_draft_reference(conn, task, workspace, intent)
            for target in intent.targets:
                if not target_allowed(task.scope, target):
                    raise ValueError(f"Intent target outside Task Scope: {target}")
            identity = _intent_identity(intent)
            if any(_intent_identity(item) == identity for item in workspace.draft.intent_proposals):
                raise ValueError("Duplicate Intent proposal")
            workspace.draft.intent_proposals.append(intent)

        return self._mutate_draft(workspace_id, mutate)

    def _mutate_draft(self, workspace_id: str, mutator) -> ReasonWorkspace:
        """Apply one draft mutation with optimistic concurrency and retry.

        Reason may issue tool calls concurrently. A plain read/modify/write
        would silently discard one call; CAS makes conflicts observable and a
        retry merges the mutation onto the newest draft.
        """
        for _attempt in range(5):
            try:
                with self.engine.begin() as conn:
                    workspace = self._require_open(conn, workspace_id)
                    mutator(conn, workspace)
                    return self.repository.save_draft(
                        conn,
                        workspace_id,
                        workspace.draft,
                        expected_updated_at=workspace.updated_at,
                        now=utcnow(),
                    )
            except WorkspaceDraftConflict:
                time.sleep(0.01)
                continue
            except OperationalError as exc:
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise
                time.sleep(0.01)
                continue
        raise ValueError("Reason Workspace changed concurrently; retry the mutation")

    def commit(self, workspace_id: str) -> CommitResult:
        with self.engine.begin() as conn:
            workspace = self.repository.get(conn, workspace_id)
            return self._commit_loaded_workspace(conn, workspace_id, workspace, now=utcnow())

    def submit(self, workspace_id: str, draft: WorkspaceDraft) -> CommitResult:
        """Validate and commit a complete Reason plan in one transaction."""
        with self.engine.begin() as conn:
            workspace = self.repository.get(conn, workspace_id)
            if workspace is not None:
                workspace = workspace.model_copy(update={"draft": draft})
            return self._commit_loaded_workspace(conn, workspace_id, workspace, now=utcnow())

    def _commit_loaded_workspace(
        self,
        conn,
        workspace_id: str,
        workspace: ReasonWorkspace | None,
        *,
        now: str,
    ) -> CommitResult:
        if workspace is None:
            raise ValueError(f"Reason Workspace not found: {workspace_id}")
        if workspace.status == "committed":
            raise ValueError("Reason Workspace already committed")
        if workspace.status != "open":
            raise ValueError("Reason Workspace is not open")
        task = self.task_repository.get(conn, workspace.task_id)
        if task is None:
            raise ValueError(f"Task not found: {workspace.task_id}")
        if task.legacy_readonly or task.status != "running":
            raise ValueError("Task is not writable")

        self._normalize_workspace_sources(conn, workspace)
        self._validate_limits(conn, workspace)
        self._validate_todo_sources(conn, task.project_id, workspace)
        existing_todos = self._validate_existing_todos(conn, workspace)
        normalized_targets = self._validate_intents(conn, task, workspace, existing_todos)

        created_todo_ids: list[str] = []
        todo_ref_map: dict[str, str] = {}
        for draft_todo in workspace.draft.todo_adds:
            created = self.todo_repository.create(
                conn,
                workspace.task_id,
                CreateTodo(
                    title=draft_todo.title,
                    category=draft_todo.category,
                    theme=draft_todo.theme,
                    priority=draft_todo.priority,
                    sources=draft_todo.sources,
                    actor=workspace.worker,
                ),
                now=now,
            )
            created_todo_ids.append(created.id)
            todo_ref_map[draft_todo.ref] = created.id

        for draft_update in workspace.draft.todo_updates:
            values = {
                key: value
                for key, value in draft_update.model_dump(
                    exclude={"todo_id", "source_adds"}
                ).items()
                if value is not None
            }
            values["updated_at"] = now
            if draft_update.status == "completed":
                values["completed_at"] = now
            elif draft_update.status is not None:
                values["completed_at"] = None
            self.todo_repository.update(
                conn, workspace.task_id, draft_update.todo_id, values
            )
            self.todo_repository.add_sources(
                conn,
                workspace.task_id,
                draft_update.todo_id,
                draft_update.source_adds,
            )

        created_intent_ids: list[str] = []
        worker_pool_json = json.dumps(task.worker_pool, ensure_ascii=False)
        for index, proposal in enumerate(workspace.draft.intent_proposals):
            todo_id = proposal.todo_id or todo_ref_map[proposal.todo_ref or ""]
            intent_id = allocate_readable_id(
                conn,
                scope=workspace.task_id,
                kind="intent",
                prefix="i",
            )
            conn.execute(
                intents.insert().values(
                    id=intent_id,
                    task_id=workspace.task_id,
                    todo_id=todo_id,
                    description=proposal.description,
                    status="pending",
                    creator=workspace.worker,
                    worker_pool_json=worker_pool_json,
                    targets_json=json.dumps(normalized_targets[index], ensure_ascii=False),
                    execute_timeout_seconds=proposal.execute_seconds,
                    conclude_timeout_seconds=proposal.conclude_seconds,
                    created_at=now,
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
                .where(todos.c.task_id == workspace.task_id, todos.c.id == todo_id)
                .where(todos.c.status == "pending")
                .values(status="active", updated_at=now)
            )
            created_intent_ids.append(intent_id)

        open_explore_intents = conn.execute(
            select(func.count())
            .select_from(intents)
            .where(
                intents.c.task_id == workspace.task_id,
                intents.c.id != "i001",
                intents.c.status.in_(["pending", "claimed", "running", "concluding"]),
            )
        ).scalar_one()
        conn.execute(
            update(tasks)
            .where(tasks.c.id == workspace.task_id)
            .values(
                # Incremental Reason may intentionally add no Intent when the
                # queue already meets its target depth.  Preserve Explore while
                # any executable Intent remains; only reopen Reason immediately
                # when the queue is truly empty and the Goal was not completed.
                phase="explore" if open_explore_intents else "reason",
                updated_at=now,
            )
        )
        # Persist the exact submitted plan in the committed Workspace for
        # diagnostics and reproducibility, then mark it committed.
        conn.execute(
            update(reason_workspaces)
            .where(reason_workspaces.c.id == workspace_id)
            .values(draft_json=workspace.draft.model_dump_json(), updated_at=now)
        )
        committed = self.repository.mark_committed(conn, workspace_id, now=now)
        self.audit.record_in(
            conn,
            RecordAuditEvent(
                project_id=task.project_id,
                task_id=workspace.task_id,
                workspace_id=workspace_id,
                worker=workspace.worker,
                event_type="reason_workspace.committed",
                message=f"Reason Workspace {workspace_id} committed",
                payload={
                    "created_todo_ids": created_todo_ids,
                    "created_intent_ids": created_intent_ids,
                },
            ),
        )
        return CommitResult(
            workspace=committed,
            created_todo_ids=created_todo_ids,
            created_intent_ids=created_intent_ids,
        )

    def _require_open(self, conn, workspace_id: str) -> ReasonWorkspace:
        workspace = self.repository.get(conn, workspace_id)
        if workspace is None:
            raise ValueError(f"Reason Workspace not found: {workspace_id}")
        if workspace.status != "open":
            raise ValueError("Reason Workspace is not open")
        return workspace

    def _validate_limits(self, conn, workspace: ReasonWorkspace) -> None:
        committed_rounds = conn.execute(
            select(func.count())
            .select_from(reason_workspaces)
            .where(
                reason_workspaces.c.task_id == workspace.task_id,
                reason_workspaces.c.status == "committed",
            )
        ).scalar_one()
        max_adds = 10 if committed_rounds == 0 else 5
        if len(workspace.draft.todo_adds) > max_adds:
            raise ValueError(f"Reason may add at most {max_adds} Todos in this round")
        if len(workspace.draft.todo_updates) > 10:
            raise ValueError("Reason may update at most 10 Todos in one round")
        if len(workspace.draft.intent_proposals) > 4:
            raise ValueError("Reason may propose at most 4 Intents in one round")
        unfinished = conn.execute(
            select(func.count())
            .select_from(todos)
            .where(
                todos.c.task_id == workspace.task_id,
                todos.c.status.in_(["pending", "active"]),
            )
        ).scalar_one()
        if unfinished + len(workspace.draft.todo_adds) > 100:
            raise ValueError("Task may not exceed 100 unfinished Todos")

    def _validate_todo_sources(self, conn, project_id: str, workspace: ReasonWorkspace) -> None:
        refs: set[str] = set()
        for draft in workspace.draft.todo_adds:
            if draft.ref in refs:
                raise ValueError(f"Duplicate Todo draft ref: {draft.ref}")
            refs.add(draft.ref)
            for source in draft.sources:
                if not self.todo_repository.source_exists(
                    conn,
                    task_id=workspace.task_id,
                    project_id=project_id,
                    source=source,
                ):
                    raise ValueError(f"Todo source not found: {source.type}:{source.id}")
            if not self._fact_source_ids(draft.sources):
                raise ValueError(
                    f"Todo {draft.ref} requires at least one Fact source or origin anchor"
                )
        for draft in workspace.draft.todo_updates:
            for source in draft.source_adds:
                if not self.todo_repository.source_exists(
                    conn,
                    task_id=workspace.task_id,
                    project_id=project_id,
                    source=source,
                ):
                    raise ValueError(f"Todo source not found: {source.type}:{source.id}")

    def _validate_existing_todos(self, conn, workspace: ReasonWorkspace) -> dict[str, str]:
        ids = {
            update.todo_id for update in workspace.draft.todo_updates
        } | {
            proposal.todo_id
            for proposal in workspace.draft.intent_proposals
            if proposal.todo_id is not None
        }
        result: dict[str, str] = {}
        for todo_id in ids:
            todo = self.todo_repository.get(conn, workspace.task_id, todo_id)
            if todo is None:
                raise ValueError(f"Todo not found: {todo_id}")
            result[todo_id] = todo.status
        return result

    def _validate_intent_draft_reference(
        self,
        conn,
        task,
        workspace: ReasonWorkspace,
        proposal: DraftIntent,
    ) -> None:
        if proposal.todo_ref and not any(
            draft.ref == proposal.todo_ref for draft in workspace.draft.todo_adds
        ):
            raise ValueError(f"Todo draft ref not found: {proposal.todo_ref}")
        if proposal.todo_id:
            todo = self.todo_repository.get(conn, workspace.task_id, proposal.todo_id)
            if todo is None:
                raise ValueError(f"Todo not found: {proposal.todo_id}")
            if todo.status in {"completed", "rejected", "obsolete"}:
                raise ValueError(f"Todo is not actionable: {proposal.todo_id}")
        for fact_id in proposal.from_facts:
            if fact_id == "goal":
                raise ValueError("Goal cannot be an Intent source")
            exists = conn.execute(
                select(facts.c.id).where(
                    facts.c.task_id == workspace.task_id,
                    facts.c.id == fact_id,
                    facts.c.archived.is_(False),
                )
            ).scalar_one_or_none()
            if exists is None:
                raise ValueError(f"Fact not found: {fact_id}")
        if proposal.todo_ref:
            todo_sources_for_proposal = next(
                draft.sources
                for draft in workspace.draft.todo_adds
                if draft.ref == proposal.todo_ref
            )
        else:
            existing = self.todo_repository.get(
                conn, workspace.task_id, proposal.todo_id or ""
            )
            if existing is None:
                raise ValueError(f"Todo not found: {proposal.todo_id}")
            pending_source_adds = next(
                (
                    update_item.source_adds
                    for update_item in workspace.draft.todo_updates
                    if update_item.todo_id == existing.id
                ),
                [],
            )
            todo_sources_for_proposal = self._augment_sources(
                conn,
                workspace.task_id,
                [*existing.sources, *pending_source_adds],
            )
        self._validate_intent_source_alignment(
            conn,
            workspace.task_id,
            proposal,
            todo_sources_for_proposal,
        )

    def _validate_intents(self, conn, task, workspace, existing_todos: dict[str, str]) -> list[list[str]]:
        draft_refs = {todo.ref for todo in workspace.draft.todo_adds}
        draft_sources = {todo.ref: todo.sources for todo in workspace.draft.todo_adds}
        source_adds = {
            update.todo_id: update.source_adds for update in workspace.draft.todo_updates
        }
        initial_bootstrap_fact = self._initial_bootstrap_fact(conn, workspace.task_id)
        normalized_target_sets: list[list[str]] = []
        for proposal in workspace.draft.intent_proposals:
            if proposal.todo_ref and proposal.todo_ref not in draft_refs:
                raise ValueError(f"Todo draft ref not found: {proposal.todo_ref}")
            if proposal.todo_id and existing_todos.get(proposal.todo_id) in {
                "completed",
                "rejected",
                "obsolete",
            }:
                raise ValueError(f"Todo is not actionable: {proposal.todo_id}")
            for fact_id in proposal.from_facts:
                if fact_id == "goal":
                    raise ValueError("Goal cannot be an Intent source")
                exists = conn.execute(
                    select(facts.c.id).where(
                        facts.c.task_id == workspace.task_id,
                        facts.c.id == fact_id,
                        facts.c.archived.is_(False),
                    )
                ).scalar_one_or_none()
                if exists is None:
                    raise ValueError(f"Fact not found: {fact_id}")
            if proposal.todo_ref:
                todo_sources_for_proposal = draft_sources[proposal.todo_ref]
            else:
                existing = self.todo_repository.get(
                    conn, workspace.task_id, proposal.todo_id or ""
                )
                if existing is None:
                    raise ValueError(f"Todo not found: {proposal.todo_id}")
                todo_sources_for_proposal = self._augment_sources(
                    conn,
                    workspace.task_id,
                    [*existing.sources, *source_adds.get(existing.id, [])],
                )
            self._validate_intent_source_alignment(
                conn,
                workspace.task_id,
                proposal,
                todo_sources_for_proposal,
                initial_bootstrap_fact=initial_bootstrap_fact,
            )
            normalized: list[str] = []
            for target in proposal.targets:
                if not target_allowed(task.scope, target):
                    raise ValueError(f"Intent target outside Task Scope: {target}")
                host = normalize_intent_target(target)
                if host not in normalized:
                    normalized.append(host)
            normalized_target_sets.append(normalized)
        return normalized_target_sets

    def _validate_intent_source_alignment(
        self,
        conn,
        task_id: str,
        proposal: DraftIntent,
        todo_sources_for_proposal: list[TodoSource],
        *,
        initial_bootstrap_fact: str | None = None,
    ) -> None:
        allowed_fact_ids = self._fact_source_ids(todo_sources_for_proposal)
        todo_ref = proposal.todo_ref or proposal.todo_id
        if not allowed_fact_ids:
            raise ValueError(f"Todo {todo_ref} has no Fact source or origin anchor")
        unrelated = sorted(set(proposal.from_facts) - allowed_fact_ids)
        if unrelated:
            raise ValueError(
                f"Intent sources must belong to Todo {todo_ref}; unrelated Facts: "
                f"{', '.join(unrelated)}"
            )
        bootstrap_fact = (
            initial_bootstrap_fact
            if initial_bootstrap_fact is not None
            else self._initial_bootstrap_fact(conn, task_id)
        )
        if bootstrap_fact and bootstrap_fact not in proposal.from_facts:
            raise ValueError(
                "The first Reason round must dispatch Intents from the Bootstrap "
                f"Fact {bootstrap_fact}"
            )

    def _normalize_workspace_sources(self, conn, workspace: ReasonWorkspace) -> None:
        """Turn evidence references into durable graph anchors before validation.

        Artifacts and vulnerabilities are useful evidence, but Intents can only
        form graph edges from Facts. Their producing Intent therefore supplies
        the corresponding result Fact. During the first Reason round every new
        Todo also consumes the Bootstrap result, even when the model only cited
        a Hint or Project Knowledge entry.
        """
        initial_bootstrap_fact = self._initial_bootstrap_fact(conn, workspace.task_id)
        workspace.draft.todo_adds = [
            todo.model_copy(
                update={
                    "sources": self._augment_sources(
                        conn,
                        workspace.task_id,
                        todo.sources,
                        required_bootstrap_fact=initial_bootstrap_fact,
                    )
                }
            )
            for todo in workspace.draft.todo_adds
        ]
        workspace.draft.todo_updates = [
            update_item.model_copy(
                update={
                    "source_adds": self._augment_sources(
                        conn,
                        workspace.task_id,
                        update_item.source_adds,
                    )
                }
            )
            for update_item in workspace.draft.todo_updates
        ]

    def _augment_sources(
        self,
        conn,
        task_id: str,
        sources: list[TodoSource],
        *,
        required_bootstrap_fact: str | None = None,
    ) -> list[TodoSource]:
        normalized: list[TodoSource] = []
        seen: set[tuple[str, str]] = set()

        def append(source: TodoSource) -> None:
            key = (source.type, source.id)
            if key not in seen:
                seen.add(key)
                normalized.append(source)

        for source in sources:
            append(source)
            producing_fact = self._producing_fact_for_source(conn, task_id, source)
            if producing_fact:
                append(TodoSource(type="fact", id=producing_fact))
        if required_bootstrap_fact:
            append(TodoSource(type="fact", id=required_bootstrap_fact))
        return normalized

    @staticmethod
    def _fact_source_ids(sources: list[TodoSource]) -> set[str]:
        return {
            "origin" if source.type == "origin" else source.id
            for source in sources
            if source.type in {"fact", "origin"}
        }

    @staticmethod
    def _producing_fact_for_source(conn, task_id: str, source: TodoSource) -> str | None:
        if source.type == "artifact":
            statement = (
                select(intents.c.to_fact_id)
                .select_from(
                    artifacts.join(
                        intents,
                        (intents.c.task_id == artifacts.c.task_id)
                        & (intents.c.id == artifacts.c.intent_id),
                    )
                )
                .where(
                    artifacts.c.task_id == task_id,
                    artifacts.c.id == source.id,
                    artifacts.c.archived.is_(False),
                    intents.c.to_fact_id.is_not(None),
                )
            )
        elif source.type == "vulnerability":
            statement = (
                select(intents.c.to_fact_id)
                .select_from(
                    vulnerabilities.join(
                        intents,
                        (intents.c.task_id == vulnerabilities.c.task_id)
                        & (intents.c.id == vulnerabilities.c.intent_id),
                    )
                )
                .where(
                    vulnerabilities.c.task_id == task_id,
                    vulnerabilities.c.id == source.id,
                    vulnerabilities.c.archived.is_(False),
                    intents.c.to_fact_id.is_not(None),
                )
            )
        else:
            return None
        return conn.execute(statement).scalar_one_or_none()

    @staticmethod
    def _initial_bootstrap_fact(conn, task_id: str) -> str | None:
        committed_rounds = conn.execute(
            select(func.count())
            .select_from(reason_workspaces)
            .where(
                reason_workspaces.c.task_id == task_id,
                reason_workspaces.c.status == "committed",
            )
        ).scalar_one()
        if committed_rounds:
            return None
        return conn.execute(
            select(intents.c.to_fact_id).where(
                intents.c.task_id == task_id,
                intents.c.id == "i001",
                intents.c.to_fact_id.is_not(None),
            )
        ).scalar_one_or_none()


def _intent_identity(intent: DraftIntent) -> tuple[object, ...]:
    return (
        intent.todo_ref or "",
        intent.todo_id or "",
        tuple(sorted(intent.from_facts)),
        " ".join(intent.description.split()),
        tuple(sorted({normalize_intent_target(target) for target in intent.targets})),
        intent.execute_seconds,
        intent.conclude_seconds,
    )
