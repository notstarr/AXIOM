from __future__ import annotations

from sqlalchemy import Connection, and_, select, update

from axiom.infrastructure.database.metadata import (
    artifacts,
    facts,
    project_knowledge,
    task_hints,
    todo_sources,
    todos,
    vulnerabilities,
)
from axiom.modules.common.ids import allocate_readable_id
from axiom.modules.todos.schemas import CreateTodo, Todo, TodoSource


class TodoRepository:
    def create(self, conn: Connection, task_id: str, command: CreateTodo, *, now: str) -> Todo:
        todo_id = allocate_readable_id(
            conn,
            scope=task_id,
            kind="todo",
            prefix="td",
        )
        values = {
            "id": todo_id,
            "task_id": task_id,
            "title": command.title,
            "category": command.category,
            "theme": command.theme,
            "priority": command.priority,
            "status": "pending",
            "created_by": command.actor,
            "status_reason": "",
            "created_at": now,
            "updated_at": now,
            "completed_at": None,
            "archived": False,
        }
        conn.execute(todos.insert().values(**values))
        conn.execute(
            todo_sources.insert(),
            [
                {
                    "task_id": task_id,
                    "todo_id": todo_id,
                    "source_type": source.type,
                    "source_id": source.id,
                }
                for source in command.sources
            ],
        )
        return Todo(**values, sources=command.sources)

    def get(self, conn: Connection, task_id: str, todo_id: str) -> Todo | None:
        row = conn.execute(
            select(todos).where(todos.c.task_id == task_id, todos.c.id == todo_id)
        ).mappings().one_or_none()
        return self._to_model(conn, row) if row is not None else None

    def list(self, conn: Connection, task_id: str) -> list[Todo]:
        rows = conn.execute(
            select(todos)
            .where(todos.c.task_id == task_id, todos.c.archived.is_(False))
            .order_by(todos.c.status, todos.c.priority.desc(), todos.c.created_at, todos.c.id)
        ).mappings()
        return [self._to_model(conn, row) for row in rows]

    def update(self, conn: Connection, task_id: str, todo_id: str, values: dict) -> Todo | None:
        result = conn.execute(
            update(todos)
            .where(todos.c.task_id == task_id, todos.c.id == todo_id)
            .values(**values)
        )
        if result.rowcount == 0:
            return None
        return self.get(conn, task_id, todo_id)

    def add_sources(
        self,
        conn: Connection,
        task_id: str,
        todo_id: str,
        sources: list[TodoSource],
    ) -> None:
        existing = {
            (row.source_type, row.source_id)
            for row in conn.execute(
                select(todo_sources.c.source_type, todo_sources.c.source_id).where(
                    todo_sources.c.task_id == task_id,
                    todo_sources.c.todo_id == todo_id,
                )
            )
        }
        values = [
            {
                "task_id": task_id,
                "todo_id": todo_id,
                "source_type": source.type,
                "source_id": source.id,
            }
            for source in sources
            if (source.type, source.id) not in existing
        ]
        if values:
            conn.execute(todo_sources.insert(), values)

    def source_exists(
        self,
        conn: Connection,
        *,
        task_id: str,
        project_id: str,
        source: TodoSource,
    ) -> bool:
        if source.type == "origin":
            return source.id == "origin" and self._exists(
                conn, select(facts.c.id).where(facts.c.task_id == task_id, facts.c.id == "origin")
            )
        if source.type == "fact":
            statement = select(facts.c.id).where(facts.c.task_id == task_id, facts.c.id == source.id)
        elif source.type == "hint":
            statement = select(task_hints.c.id).where(
                task_hints.c.task_id == task_id, task_hints.c.id == source.id
            )
        elif source.type == "knowledge":
            statement = select(project_knowledge.c.id).where(
                project_knowledge.c.project_id == project_id,
                project_knowledge.c.id == source.id,
            )
        elif source.type == "artifact":
            statement = select(artifacts.c.id).where(
                artifacts.c.task_id == task_id, artifacts.c.id == source.id
            )
        elif source.type == "vulnerability":
            statement = select(vulnerabilities.c.id).where(
                vulnerabilities.c.task_id == task_id,
                vulnerabilities.c.id == source.id,
            )
        else:
            return False
        return self._exists(conn, statement)

    @staticmethod
    def _exists(conn: Connection, statement) -> bool:
        return conn.execute(statement).scalar_one_or_none() is not None

    @staticmethod
    def _sources(conn: Connection, task_id: str, todo_id: str) -> list[TodoSource]:
        rows = conn.execute(
            select(todo_sources.c.source_type, todo_sources.c.source_id)
            .where(todo_sources.c.task_id == task_id, todo_sources.c.todo_id == todo_id)
            .order_by(todo_sources.c.source_type, todo_sources.c.source_id)
        )
        return [TodoSource(type=row.source_type, id=row.source_id) for row in rows]

    def _to_model(self, conn: Connection, row) -> Todo:
        return Todo(**row, sources=self._sources(conn, row["task_id"], row["id"]))
