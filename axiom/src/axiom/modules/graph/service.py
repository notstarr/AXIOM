from __future__ import annotations

from sqlalchemy import Engine

from axiom.modules.graph.repository import GraphRepository
from axiom.modules.graph.schemas import Fact, Intent, TaskGraph, UpdateFact, UpdateIntent
from axiom.modules.tasks.repository import TaskRepository
from axiom.modules.tasks.tables import tasks
from axiom.shared.clock import utcnow
from axiom.shared.errors import ConflictError, NotFoundError


class GraphService:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.repository = GraphRepository()
        self.tasks = TaskRepository()

    def get(self, task_id: str) -> TaskGraph:
        with self.engine.connect() as conn:
            if self.tasks.get(conn, task_id) is None:
                raise NotFoundError(f"Task not found: {task_id}")
            return self.repository.graph(conn, task_id)

    def fact(self, task_id: str, fact_id: str) -> Fact:
        with self.engine.connect() as conn:
            fact = self.repository.get_fact(conn, task_id, fact_id)
        if fact is None:
            raise NotFoundError(f"Fact not found: {task_id}/{fact_id}")
        return fact

    def intent(self, task_id: str, intent_id: str) -> Intent:
        with self.engine.connect() as conn:
            intent = self.repository.get_intent(conn, task_id, intent_id)
        if intent is None:
            raise NotFoundError(f"Intent not found: {task_id}/{intent_id}")
        return intent

    def update_fact(self, task_id: str, fact_id: str, command: UpdateFact) -> Fact:
        with self.engine.begin() as conn:
            task = self._mutable_task(conn, task_id)
            current = self.repository.get_fact(conn, task_id, fact_id)
            if current is None:
                raise NotFoundError(f"Fact not found: {task_id}/{fact_id}")
            if fact_id == "origin" and command.description is not None:
                raise ConflictError("Origin Fact is managed by Task origin and cannot be edited")
            if fact_id in {"origin", "goal"} and command.archived:
                raise ConflictError(f"{fact_id.title()} Fact cannot be archived")
            if command.archived and self.repository.fact_has_visible_intents(conn, task_id, fact_id):
                raise ConflictError("Fact is still referenced by visible Intents")
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
        with self.engine.begin() as conn:
            self._mutable_task(conn, task_id)
            current = self.repository.get_intent(conn, task_id, intent_id)
            if current is None:
                raise NotFoundError(f"Intent not found: {task_id}/{intent_id}")
            if command.description is not None and current.status in {
                "claimed",
                "running",
                "concluding",
            }:
                raise ConflictError("An active Intent description cannot be edited")
            if command.archived and current.status in {"pending", "claimed", "running", "concluding"}:
                raise ConflictError("An unfinished Intent cannot be archived")
            values = command.model_dump(exclude_none=True)
            updated = self.repository.update_intent(conn, task_id, intent_id, values)
            assert updated is not None
            return updated

    def _mutable_task(self, conn, task_id: str):
        task = self.tasks.get(conn, task_id)
        if task is None:
            raise NotFoundError(f"Task not found: {task_id}")
        if task.legacy_readonly:
            raise ConflictError("Legacy Task is read-only")
        return task
