from __future__ import annotations

from sqlalchemy import Connection, select, update

from axiom.modules.artifacts.tables import artifacts
from axiom.modules.context.tables import context_revisions
from axiom.modules.graph.tables import facts, intents
from axiom.modules.goals.tables import goal_evaluations, task_goal_cycles
from axiom.modules.knowledge.tables import project_knowledge
from axiom.modules.tasks.tables import task_hints, tasks
from axiom.modules.todos.tables import todos
from axiom.modules.vulnerabilities.tables import vulnerabilities
from axiom.modules.redteam.tables import red_team_chains, red_team_results


class ContextRepository:
    def task(self, conn: Connection, task_id: str):
        return conn.execute(select(tasks).where(tasks.c.id == task_id)).mappings().one_or_none()

    def facts(self, conn: Connection, task_id: str, *, include_archived: bool = False):
        query = select(facts).where(facts.c.task_id == task_id)
        if not include_archived:
            query = query.where(facts.c.archived.is_(False))
        return conn.execute(query.order_by(facts.c.created_at, facts.c.id)).mappings().all()

    def intents(self, conn: Connection, task_id: str, *, include_archived: bool = False):
        query = select(intents).where(intents.c.task_id == task_id)
        if not include_archived:
            query = query.where(intents.c.archived.is_(False))
        return conn.execute(query.order_by(intents.c.created_at, intents.c.id)).mappings().all()

    def artifacts(self, conn: Connection, task_id: str, *, include_archived: bool = False):
        query = select(artifacts).where(artifacts.c.task_id == task_id)
        if not include_archived:
            query = query.where(artifacts.c.archived.is_(False))
        return conn.execute(query.order_by(artifacts.c.created_at, artifacts.c.id)).mappings().all()

    def todos(self, conn: Connection, task_id: str, *, include_archived: bool = False):
        query = select(todos).where(todos.c.task_id == task_id)
        if not include_archived:
            query = query.where(todos.c.archived.is_(False))
        return conn.execute(
            query.order_by(todos.c.status, todos.c.priority.desc(), todos.c.id)
        ).mappings().all()

    def hints(self, conn: Connection, task_id: str, *, include_archived: bool = False):
        query = select(task_hints).where(task_hints.c.task_id == task_id)
        if not include_archived:
            query = query.where(task_hints.c.archived.is_(False))
        return conn.execute(
            query.order_by(task_hints.c.created_at, task_hints.c.id)
        ).mappings().all()

    def vulnerabilities(self, conn: Connection, task_id: str, *, include_archived: bool = False):
        query = select(vulnerabilities).where(vulnerabilities.c.task_id == task_id)
        if not include_archived:
            query = query.where(vulnerabilities.c.archived.is_(False))
        return conn.execute(
            query.order_by(vulnerabilities.c.updated_at, vulnerabilities.c.id)
        ).mappings().all()

    def red_team_chains(self, conn: Connection, task_id: str):
        return conn.execute(
            select(red_team_chains).where(red_team_chains.c.task_id == task_id).order_by(red_team_chains.c.created_at, red_team_chains.c.id)
        ).mappings().all()

    def red_team_results(self, conn: Connection, task_id: str):
        return conn.execute(
            select(red_team_results).where(red_team_results.c.task_id == task_id).order_by(red_team_results.c.created_at, red_team_results.c.id)
        ).mappings().all()

    def knowledge(self, conn: Connection, project_id: str, *, include_archived: bool = False):
        query = select(project_knowledge).where(
            project_knowledge.c.project_id == project_id,
            project_knowledge.c.enabled.is_(True),
        )
        if not include_archived:
            query = query.where(project_knowledge.c.archived.is_(False))
        return conn.execute(
            query.order_by(project_knowledge.c.updated_at, project_knowledge.c.id)
        ).mappings().all()

    def goal_evaluations(self, conn: Connection, task_id: str):
        return conn.execute(
            select(goal_evaluations)
            .where(goal_evaluations.c.task_id == task_id)
            .order_by(goal_evaluations.c.created_at, goal_evaluations.c.id)
        ).mappings().all()

    def goal_cycles(self, conn: Connection, task_id: str):
        return conn.execute(
            select(task_goal_cycles)
            .where(task_goal_cycles.c.task_id == task_id)
            .order_by(task_goal_cycles.c.sequence)
        ).mappings().all()

    def insert_revision(self, conn: Connection, values: dict) -> None:
        conn.execute(context_revisions.insert().values(**values))

    def mark_reclaimed(self, conn: Connection, task_id: str, revision: int, deleted_at: str) -> bool:
        result = conn.execute(
            update(context_revisions)
            .where(
                context_revisions.c.task_id == task_id,
                context_revisions.c.revision == revision,
                context_revisions.c.storage_state == "present",
            )
            .values(storage_state="reclaimed", deleted_at=deleted_at)
        )
        return bool(result.rowcount)

    def for_lease(
        self,
        conn: Connection,
        *,
        task_id: str,
        intent_id: str | None,
        reason_run_id: str | None,
        plan_run_id: str | None,
        review_run_id: str | None,
        lease_id: str,
    ):
        return conn.execute(
            select(context_revisions).where(
                context_revisions.c.task_id == task_id,
                context_revisions.c.intent_id == intent_id,
                context_revisions.c.reason_run_id == reason_run_id,
                context_revisions.c.plan_run_id == plan_run_id,
                context_revisions.c.review_run_id == review_run_id,
                context_revisions.c.lease_id == lease_id,
            )
        ).mappings().one_or_none()
