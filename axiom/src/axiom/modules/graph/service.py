from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Engine

from axiom.modules.audit.schemas import RecordAuditEvent
from axiom.modules.graph.repository import GraphRepository
from axiom.modules.graph.schemas import (
    Fact,
    GraphChildren,
    GraphCounts,
    Intent,
    TaskGraph,
    UpdateFact,
    UpdateIntent,
)
from axiom.modules.tasks.repository import TaskRepository
from axiom.modules.tasks.tables import tasks
from axiom.shared.clock import utcnow
from axiom.shared.errors import ConflictError, NotFoundError

if TYPE_CHECKING:
    from axiom.modules.archives.service import ArchiveService
    from axiom.modules.audit.service import AuditService


class GraphService:
    def __init__(
        self,
        engine: Engine,
        *,
        archives: ArchiveService | None = None,
        audit: AuditService | None = None,
    ):
        self.engine = engine
        self.repository = GraphRepository()
        self.tasks = TaskRepository()
        self.archives = archives
        self.audit = audit

    def get(self, task_id: str, scope: str = "full") -> TaskGraph:
        with self.engine.connect() as conn:
            task = self.tasks.get(conn, task_id)
            if task is None:
                raise NotFoundError(f"Task not found: {task_id}")
            if task.archived:
                raise ConflictError("已归档任务的图谱不可见，请先恢复任务")
            return self.repository.graph(conn, task_id, scope=scope)

    def counts(self, task_id: str) -> GraphCounts:
        with self.engine.connect() as conn:
            task = self.tasks.get(conn, task_id)
            if task is None:
                raise NotFoundError(f"Task not found: {task_id}")
            return self.repository.counts(conn, task_id)

    def children(self, task_id: str, node_id: str, limit: int = 100) -> GraphChildren:
        with self.engine.connect() as conn:
            task = self.tasks.get(conn, task_id)
            if task is None:
                raise NotFoundError(f"Task not found: {task_id}")
            if task.archived:
                raise ConflictError("已归档任务的图谱不可见，请先恢复任务")
            return self.repository.children(conn, task_id, node_id, limit=limit)

    def fact(self, task_id: str, fact_id: str) -> Fact:
        with self.engine.connect() as conn:
            task = self.tasks.get(conn, task_id)
            if task is None:
                raise NotFoundError(f"Task not found: {task_id}")
            if task.archived:
                raise ConflictError("已归档任务的图谱不可见，请先恢复任务")
            fact = self.repository.get_fact(conn, task_id, fact_id)
        if fact is None:
            raise NotFoundError(f"Fact not found: {task_id}/{fact_id}")
        return fact

    def intent(self, task_id: str, intent_id: str) -> Intent:
        with self.engine.connect() as conn:
            task = self.tasks.get(conn, task_id)
            if task is None:
                raise NotFoundError(f"Task not found: {task_id}")
            if task.archived:
                raise ConflictError("已归档任务的图谱不可见，请先恢复任务")
            intent = self.repository.get_intent(conn, task_id, intent_id)
        if intent is None:
            raise NotFoundError(f"Intent not found: {task_id}/{intent_id}")
        return intent

    def update_fact(self, task_id: str, fact_id: str, command: UpdateFact) -> Fact:
        if command.archived is not None:
            if command.description is not None:
                raise ConflictError("Fact content and archive state must be changed separately")
            if self.archives is None:
                raise ConflictError("Archive service is unavailable")
            if command.archived:
                self.archives.create_compat(task_id, root_type="fact", root_id=fact_id)
            else:
                self.archives.restore_root(task_id, root_type="fact", root_id=fact_id)
            return self.fact(task_id, fact_id)
        with self.engine.begin() as conn:
            task = self._mutable_task(conn, task_id)
            current = self.repository.get_fact(conn, task_id, fact_id)
            if current is None:
                raise NotFoundError(f"Fact not found: {task_id}/{fact_id}")
            if current.archived:
                raise ConflictError(f"Archived Fact cannot be updated: {fact_id}")
            if fact_id == "origin" and command.description is not None:
                raise ConflictError("Origin Fact is managed by Task origin and cannot be edited")
            values = command.model_dump(exclude_none=True)
            updated = self.repository.update_fact(conn, task_id, fact_id, values)
            assert updated is not None
            if fact_id == "goal" and command.description is not None:
                conn.execute(
                    tasks.update()
                    .where(tasks.c.id == task.id)
                    .values(goal=command.description, updated_at=utcnow())
                )
            return updated

    def update_intent(self, task_id: str, intent_id: str, command: UpdateIntent) -> Intent:
        if command.archived is not None:
            if command.description is not None:
                raise ConflictError("Intent content and archive state must be changed separately")
            if self.archives is None:
                raise ConflictError("Archive service is unavailable")
            if command.archived:
                self.archives.create_compat(task_id, root_type="intent", root_id=intent_id)
            else:
                self.archives.restore_root(task_id, root_type="intent", root_id=intent_id)
            return self.intent(task_id, intent_id)
        with self.engine.begin() as conn:
            self._mutable_task(conn, task_id)
            current = self.repository.get_intent(conn, task_id, intent_id)
            if current is None:
                raise NotFoundError(f"Intent not found: {task_id}/{intent_id}")
            if current.archived:
                raise ConflictError(f"Archived Intent cannot be updated: {intent_id}")
            if command.description is not None and current.status in {
                "claimed",
                "running",
                "concluding",
            }:
                raise ConflictError("An active Intent description cannot be edited")
            values = command.model_dump(exclude_none=True)
            updated = self.repository.update_intent(conn, task_id, intent_id, values)
            assert updated is not None
            return updated

    def reset_intent_retries(self, task_id: str, intent_id: str) -> Intent:
        with self.engine.begin() as conn:
            task = self._mutable_task(conn, task_id)
            current = self.repository.get_intent(conn, task_id, intent_id)
            if current is None:
                raise NotFoundError(f"Intent not found: {task_id}/{intent_id}")
            if current.archived:
                raise ConflictError(f"Archived Intent cannot be updated: {intent_id}")
            if current.status != "pending":
                raise ConflictError(
                    f"Intent is not pending: {intent_id} (status={current.status})"
                )
            has_retry_debris = bool(
                current.infrastructure_retry_count > 0
                or current.retry_count > 0
                or current.failure_class
                or current.last_failure_kind
                or current.next_retry_at is not None
            )
            if not has_retry_debris:
                return current
            values = {
                "retry_count": 0,
                "infrastructure_retry_count": 0,
                "next_retry_at": None,
                "last_failure_kind": "",
                "failure_class": "",
                "activity_kind": "",
                "activity_summary": "",
                "activity_tool": "",
                "activity_started_at": None,
                "activity_updated_at": None,
                "lease_id": None,
                "claimed_at": None,
                "last_heartbeat_at": None,
            }
            updated = self.repository.update_intent(conn, task_id, intent_id, values)
            assert updated is not None
            if self.audit is not None:
                self.audit.record_in(
                    conn,
                    RecordAuditEvent(
                        project_id=task.project_id,
                        task_id=task_id,
                        intent_id=intent_id,
                        event_type="intent.retry_budget_reset",
                        message="人工重置 Intent 重试预算",
                        payload={
                            "actor": "human.frontend",
                            "previous_retry_count": current.retry_count,
                            "previous_infrastructure_retry_count": current.infrastructure_retry_count,
                            "previous_failure_class": current.failure_class,
                        },
                    ),
                )
            return updated

    def _mutable_task(self, conn, task_id: str):
        task = self.tasks.get(conn, task_id)
        if task is None:
            raise NotFoundError(f"Task not found: {task_id}")
        if task.legacy_readonly:
            raise ConflictError("Legacy Task is read-only")
        return task
