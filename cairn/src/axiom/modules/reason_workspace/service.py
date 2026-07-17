from __future__ import annotations

import json

from sqlalchemy import Engine, func, select, update

from axiom.infrastructure.database.metadata import facts, intent_sources, intents, tasks, todos
from axiom.modules.common.ids import allocate_readable_id
from axiom.modules.common.time import utcnow
from axiom.modules.reason_workspace.repository import ReasonWorkspaceRepository
from axiom.modules.reason_workspace.schemas import (
    CommitResult,
    DraftIntent,
    DraftTodo,
    DraftTodoUpdate,
    ReasonWorkspace,
)
from axiom.modules.tasks.repository import TaskRepository
from axiom.modules.tasks.scope import normalize_intent_target, target_allowed
from axiom.modules.todos.repository import TodoRepository
from axiom.modules.todos.schemas import CreateTodo


class ReasonWorkspaceService:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.repository = ReasonWorkspaceRepository()
        self.task_repository = TaskRepository()
        self.todo_repository = TodoRepository()

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
            return self.repository.open(
                conn,
                task_id=task_id,
                worker=worker,
                context_revision=context_revision,
                now=utcnow(),
            )

    def add_todo(self, workspace_id: str, todo: DraftTodo) -> ReasonWorkspace:
        with self.engine.begin() as conn:
            workspace = self._require_open(conn, workspace_id)
            if any(item.ref == todo.ref for item in workspace.draft.todo_adds):
                raise ValueError(f"Todo draft ref already exists: {todo.ref}")
            workspace.draft.todo_adds.append(todo)
            return self.repository.save_draft(conn, workspace_id, workspace.draft, now=utcnow())

    def update_todo(self, workspace_id: str, todo: DraftTodoUpdate) -> ReasonWorkspace:
        with self.engine.begin() as conn:
            workspace = self._require_open(conn, workspace_id)
            workspace.draft.todo_updates = [
                item for item in workspace.draft.todo_updates if item.todo_id != todo.todo_id
            ]
            workspace.draft.todo_updates.append(todo)
            return self.repository.save_draft(conn, workspace_id, workspace.draft, now=utcnow())

    def propose_intent(self, workspace_id: str, intent: DraftIntent) -> ReasonWorkspace:
        with self.engine.begin() as conn:
            workspace = self._require_open(conn, workspace_id)
            workspace.draft.intent_proposals.append(intent)
            return self.repository.save_draft(conn, workspace_id, workspace.draft, now=utcnow())

    def commit(self, workspace_id: str) -> CommitResult:
        now = utcnow()
        with self.engine.begin() as conn:
            workspace = self.repository.get(conn, workspace_id)
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
                    for key, value in draft_update.model_dump(exclude={"todo_id"}).items()
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

            conn.execute(
                update(tasks)
                .where(tasks.c.id == workspace.task_id)
                .values(
                    phase="explore" if created_intent_ids else "waiting",
                    updated_at=now,
                )
            )
            committed = self.repository.mark_committed(conn, workspace_id, now=now)
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
        existing_count = conn.execute(
            select(func.count()).select_from(todos).where(todos.c.task_id == workspace.task_id)
        ).scalar_one()
        max_adds = 10 if existing_count == 0 else 5
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

    def _validate_intents(self, conn, task, workspace, existing_todos: dict[str, str]) -> list[list[str]]:
        draft_refs = {todo.ref for todo in workspace.draft.todo_adds}
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
            normalized: list[str] = []
            for target in proposal.targets:
                if not target_allowed(task.scope, target):
                    raise ValueError(f"Intent target outside Task Scope: {target}")
                host = normalize_intent_target(target)
                if host not in normalized:
                    normalized.append(host)
            normalized_target_sets.append(normalized)
        return normalized_target_sets
