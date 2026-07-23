from __future__ import annotations

from sqlalchemy import Connection, select

from axiom.modules.artifacts.tables import artifacts
from axiom.modules.context.tables import context_revisions
from axiom.modules.graph.tables import facts, intents
from axiom.modules.goals.tables import goal_evaluations
from axiom.modules.knowledge.tables import project_knowledge
from axiom.modules.tasks.tables import task_hints, tasks
from axiom.modules.todos.tables import todos
from axiom.modules.vulnerabilities.tables import vulnerabilities


class ContextRepository:
    def task(self, conn: Connection, task_id: str):
        return conn.execute(select(tasks).where(tasks.c.id == task_id)).mappings().one_or_none()

    def facts(self, conn: Connection, task_id: str):
        return conn.execute(
            select(facts)
            .where(facts.c.task_id == task_id, facts.c.archived.is_(False))
            .order_by(facts.c.created_at, facts.c.id)
        ).mappings().all()

    def intents(self, conn: Connection, task_id: str):
        return conn.execute(
            select(intents)
            .where(intents.c.task_id == task_id, intents.c.archived.is_(False))
            .order_by(intents.c.created_at, intents.c.id)
        ).mappings().all()

    def artifacts(self, conn: Connection, task_id: str):
        return conn.execute(
            select(artifacts)
            .where(artifacts.c.task_id == task_id, artifacts.c.archived.is_(False))
            .order_by(artifacts.c.created_at, artifacts.c.id)
        ).mappings().all()

    def todos(self, conn: Connection, task_id: str):
        return conn.execute(
            select(todos)
            .where(todos.c.task_id == task_id, todos.c.archived.is_(False))
            .order_by(todos.c.status, todos.c.priority.desc(), todos.c.id)
        ).mappings().all()

    def hints(self, conn: Connection, task_id: str):
        return conn.execute(
            select(task_hints)
            .where(task_hints.c.task_id == task_id, task_hints.c.archived.is_(False))
            .order_by(task_hints.c.created_at, task_hints.c.id)
        ).mappings().all()

    def vulnerabilities(self, conn: Connection, task_id: str):
        return conn.execute(
            select(vulnerabilities)
            .where(
                vulnerabilities.c.task_id == task_id,
                vulnerabilities.c.archived.is_(False),
            )
            .order_by(vulnerabilities.c.updated_at, vulnerabilities.c.id)
        ).mappings().all()

    def knowledge(self, conn: Connection, project_id: str):
        return conn.execute(
            select(project_knowledge)
            .where(
                project_knowledge.c.project_id == project_id,
                project_knowledge.c.enabled.is_(True),
                project_knowledge.c.archived.is_(False),
            )
            .order_by(project_knowledge.c.updated_at, project_knowledge.c.id)
        ).mappings().all()

    def goal_evaluations(self, conn: Connection, task_id: str):
        return conn.execute(
            select(goal_evaluations)
            .where(goal_evaluations.c.task_id == task_id)
            .order_by(goal_evaluations.c.created_at, goal_evaluations.c.id)
        ).mappings().all()

    def insert_revision(self, conn: Connection, values: dict) -> None:
        conn.execute(context_revisions.insert().values(**values))

    def for_lease(
        self,
        conn: Connection,
        *,
        task_id: str,
        intent_id: str,
        lease_id: str,
    ):
        return conn.execute(
            select(context_revisions).where(
                context_revisions.c.task_id == task_id,
                context_revisions.c.intent_id == intent_id,
                context_revisions.c.lease_id == lease_id,
            )
        ).mappings().one_or_none()
