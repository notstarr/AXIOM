from __future__ import annotations

from sqlalchemy import Connection, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from axiom.modules.artifacts.tables import artifacts
from axiom.modules.graph.tables import facts
from axiom.modules.tasks.tables import task_hints
from axiom.modules.todos.schemas import CreateTodo, Todo, TodoSource
from axiom.modules.todos.tables import todo_sources, todos
from axiom.modules.vulnerabilities.tables import vulnerabilities
from axiom.shared.ids import allocate_id


class TodoRepository:
    def create(
        self,
        conn: Connection,
        *,
        task_id: str,
        command: CreateTodo,
        now: str,
    ) -> Todo:
        todo_id = allocate_id(conn, scope=task_id, kind="todo", prefix="td")
        values = {
            "id": todo_id,
            "task_id": task_id,
            "title": command.title,
            "category": command.category,
            "theme": command.theme,
            "priority": command.priority,
            "status": command.status,
            "created_by": command.actor,
            "status_reason": command.status_reason,
            "created_at": now,
            "updated_at": now,
            "completed_at": now if command.status == "completed" else None,
            "archived": False,
        }
        conn.execute(todos.insert().values(**values))
        self.add_sources(conn, task_id=task_id, todo_id=todo_id, sources=command.sources)
        return Todo(**values, sources=command.sources)

    def get(self, conn: Connection, task_id: str, todo_id: str) -> Todo | None:
        row = conn.execute(
            select(todos).where(todos.c.task_id == task_id, todos.c.id == todo_id)
        ).mappings().one_or_none()
        return self._todo(conn, row) if row is not None else None

    def list(self, conn: Connection, task_id: str) -> list[Todo]:
        rows = conn.execute(
            select(todos)
            .where(todos.c.task_id == task_id, todos.c.archived.is_(False))
            .order_by(todos.c.status, todos.c.priority.desc(), todos.c.created_at, todos.c.id)
        ).mappings()
        return [self._todo(conn, row) for row in rows]

    def update(self, conn: Connection, task_id: str, todo_id: str, values: dict) -> Todo | None:
        result = conn.execute(
            update(todos)
            .where(todos.c.task_id == task_id, todos.c.id == todo_id)
            .values(**values)
        )
        return self.get(conn, task_id, todo_id) if result.rowcount else None

    def add_sources(
        self,
        conn: Connection,
        *,
        task_id: str,
        todo_id: str,
        sources: list[TodoSource],
    ) -> None:
        for source in sources:
            conn.execute(
                sqlite_insert(todo_sources)
                .values(
                    task_id=task_id,
                    todo_id=todo_id,
                    source_type=source.type,
                    source_id=source.id,
                )
                .on_conflict_do_nothing(
                    index_elements=["task_id", "todo_id", "source_type", "source_id"]
                )
            )

    def source_exists(self, conn: Connection, *, task_id: str, source: TodoSource) -> bool:
        if source.type == "origin":
            return source.id == "origin" and self._exists(
                conn,
                select(facts.c.id).where(facts.c.task_id == task_id, facts.c.id == "origin"),
            )
        if source.type == "fact":
            statement = select(facts.c.id).where(
                facts.c.task_id == task_id,
                facts.c.id == source.id,
                facts.c.archived.is_(False),
            )
        elif source.type == "hint":
            statement = select(task_hints.c.id).where(
                task_hints.c.task_id == task_id,
                task_hints.c.id == source.id,
                task_hints.c.archived.is_(False),
            )
        elif source.type == "artifact":
            statement = select(artifacts.c.id).where(
                artifacts.c.task_id == task_id,
                artifacts.c.id == source.id,
                artifacts.c.archived.is_(False),
            )
        elif source.type == "vulnerability":
            statement = select(vulnerabilities.c.id).where(
                vulnerabilities.c.task_id == task_id,
                vulnerabilities.c.id == source.id,
                vulnerabilities.c.archived.is_(False),
            )
        else:
            return False
        return self._exists(conn, statement)

    @staticmethod
    def _exists(conn: Connection, statement) -> bool:
        return conn.execute(statement).scalar_one_or_none() is not None

    def _todo(self, conn: Connection, row) -> Todo:
        sources = [
            TodoSource(type=item.source_type, id=item.source_id)
            for item in conn.execute(
                select(todo_sources.c.source_type, todo_sources.c.source_id)
                .where(
                    todo_sources.c.task_id == row["task_id"],
                    todo_sources.c.todo_id == row["id"],
                )
                .order_by(todo_sources.c.source_type, todo_sources.c.source_id)
            )
        ]
        return Todo.model_validate({**row, "sources": sources})
