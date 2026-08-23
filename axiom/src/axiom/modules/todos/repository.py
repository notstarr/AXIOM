from __future__ import annotations

from sqlalchemy import Connection, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from axiom.modules.artifacts.tables import artifacts
from axiom.modules.graph.tables import facts
from axiom.modules.knowledge.tables import project_knowledge
from axiom.modules.tasks.tables import task_hints, tasks
from axiom.modules.todos.schemas import CreateTodo, Todo, TodoSource
from axiom.modules.todos.tables import todo_sources, todos
from axiom.modules.vulnerabilities.tables import vulnerabilities
from axiom.shared.ids import allocate_id, id_counters


class TodoRepository:
    @staticmethod
    def _align_todo_counter(conn: Connection, task_id: str) -> None:
        """Align id_counters for todo to be strictly greater than max existing tdXXX."""
        rows = conn.execute(
            select(todos.c.id).where(todos.c.task_id == task_id)
        ).scalars().all()
        max_num = max(
            (int(value[2:]) for value in rows if value.startswith("td") and value[2:].isdigit()),
            default=0,
        )
        conn.execute(
            update(id_counters)
            .where(
                id_counters.c.scope == task_id,
                id_counters.c.kind == "todo",
            )
            .values(next_value=func.greatest(id_counters.c.next_value, max_num + 1))
        )

    def create(
        self,
        conn: Connection,
        *,
        task_id: str,
        command: CreateTodo,
        now: str,
    ) -> Todo:
        self._align_todo_counter(conn, task_id)
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
        rows = list(conn.execute(
            select(todos)
            .where(todos.c.task_id == task_id, todos.c.archived.is_(False))
            .order_by(todos.c.status, todos.c.priority.desc(), todos.c.created_at, todos.c.id)
        ).mappings())
        return self._todos(conn, rows)

    def list_page(
        self,
        conn: Connection,
        task_id: str,
        *,
        search: str = "",
        status: str | None = None,
        limit: int,
        offset: int,
    ) -> tuple[list[Todo], int]:
        query = select(todos).where(
            todos.c.task_id == task_id,
            todos.c.archived.is_(False),
        )
        if status:
            query = query.where(todos.c.status == status)
        if search.strip():
            pattern = f"%{search.strip()}%"
            query = query.where(
                or_(
                    todos.c.id.ilike(pattern),
                    todos.c.title.ilike(pattern),
                    todos.c.category.ilike(pattern),
                    todos.c.theme.ilike(pattern),
                    todos.c.status_reason.ilike(pattern),
                )
            )
        total = conn.execute(
            select(func.count()).select_from(query.order_by(None).subquery())
        ).scalar_one()
        rows = list(
            conn.execute(
                query.order_by(
                    todos.c.status,
                    todos.c.priority.desc(),
                    todos.c.created_at,
                    todos.c.id,
                )
                .limit(limit)
                .offset(offset)
            ).mappings()
        )
        return self._todos(conn, rows), int(total)

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
                pg_insert(todo_sources)
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
        elif source.type == "knowledge":
            statement = select(project_knowledge.c.id).where(
                project_knowledge.c.id == source.id,
                project_knowledge.c.enabled.is_(True),
                project_knowledge.c.archived.is_(False),
                project_knowledge.c.project_id
                == select(
                    # Resolve the task's project without broadening a
                    # knowledge reference to another project.
                    tasks.c.project_id
                )
                .where(tasks.c.id == task_id)
                .scalar_subquery(),
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

    def _todos(self, conn: Connection, rows: list) -> list[Todo]:
        if not rows:
            return []
        task_ids = {str(row["task_id"]) for row in rows}
        todo_ids = {str(row["id"]) for row in rows}
        sources_by_key: dict[tuple[str, str], list[TodoSource]] = {}
        for item in conn.execute(
            select(todo_sources).where(
                todo_sources.c.task_id.in_(task_ids),
                todo_sources.c.todo_id.in_(todo_ids),
            )
            .order_by(
                todo_sources.c.task_id,
                todo_sources.c.todo_id,
                todo_sources.c.source_type,
                todo_sources.c.source_id,
            )
        ).mappings():
            key = (str(item["task_id"]), str(item["todo_id"]))
            sources_by_key.setdefault(key, []).append(
                TodoSource(type=item["source_type"], id=item["source_id"])
            )
        return [
            Todo.model_validate(
                {
                    **row,
                    "sources": sources_by_key.get((str(row["task_id"]), str(row["id"])), []),
                }
            )
            for row in rows
        ]
