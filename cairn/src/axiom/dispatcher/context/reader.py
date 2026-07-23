from __future__ import annotations

import json
from typing import Any, Iterable

from sqlalchemy import Engine, select

from axiom.infrastructure.database.metadata import (
    artifacts,
    facts,
    intent_sources,
    intents,
    project_knowledge,
    tasks,
    todo_sources,
    todos,
    vulnerabilities,
)


ENTITY_TABLES = {
    "facts": facts,
    "intents": intents,
    "todos": todos,
    "knowledge": project_knowledge,
    "artifacts": artifacts,
    "vulnerabilities": vulnerabilities,
}


class ContextReader:
    """Read full context entities on demand without copying them into snapshots."""

    def __init__(self, engine: Engine):
        self.engine = engine

    def expand(self, task_id: str, requested: dict[str, list[str]]) -> dict[str, Any]:
        normalized = {
            kind: _unique(ids)
            for kind, ids in requested.items()
            if kind in ENTITY_TABLES and ids
        }
        with self.engine.connect() as conn:
            task = conn.execute(
                select(tasks.c.id, tasks.c.project_id).where(tasks.c.id == task_id)
            ).mappings().one_or_none()
            if task is None:
                raise ValueError(f"Task not found: {task_id}")

            result: dict[str, Any] = {kind: [] for kind in ENTITY_TABLES}
            missing: dict[str, list[str]] = {}
            for kind, ids in normalized.items():
                rows = self._rows(conn, task_id, task["project_id"], kind, ids)
                by_id = {str(row["id"]): row for row in rows}
                result[kind] = [by_id[item] for item in ids if item in by_id]
                absent = [item for item in ids if item not in by_id]
                if absent:
                    missing[kind] = absent
            result["missing"] = missing
            return result

    def related_intent(self, task_id: str, intent_id: str) -> dict[str, Any]:
        with self.engine.connect() as conn:
            task = conn.execute(
                select(tasks.c.id, tasks.c.project_id).where(tasks.c.id == task_id)
            ).mappings().one_or_none()
            if task is None:
                raise ValueError(f"Task not found: {task_id}")
            intent_row = conn.execute(
                select(intents).where(intents.c.task_id == task_id, intents.c.id == intent_id)
            ).mappings().one_or_none()
            if intent_row is None:
                raise ValueError(f"Intent not found: {intent_id}")

            intent = self._intent(conn, intent_row)
            fact_ids = list(intent["from_facts"])
            if intent.get("to_fact_id"):
                fact_ids.append(intent["to_fact_id"])
            fact_rows = self._rows(conn, task_id, task["project_id"], "facts", fact_ids)

            todo = None
            todo_source_rows: list[dict[str, Any]] = []
            if intent.get("todo_id"):
                rows = self._rows(
                    conn,
                    task_id,
                    task["project_id"],
                    "todos",
                    [intent["todo_id"]],
                )
                todo = rows[0] if rows else None
                if todo:
                    todo_source_rows = list(todo.get("sources") or [])

            producer_intent_ids = list(
                conn.execute(
                    select(intents.c.id).where(
                        intents.c.task_id == task_id,
                        intents.c.to_fact_id.in_(fact_ids or [""]),
                    )
                ).scalars()
            )
            related_intent_ids = _unique([intent_id, *producer_intent_ids])
            artifact_ids = [
                item["id"] for item in todo_source_rows if item.get("type") == "artifact"
            ]
            vulnerability_ids = [
                item["id"] for item in todo_source_rows if item.get("type") == "vulnerability"
            ]
            knowledge_ids = [
                item["id"] for item in todo_source_rows if item.get("type") == "knowledge"
            ]
            hint_ids = [item["id"] for item in todo_source_rows if item.get("type") == "hint"]

            artifact_rows = list(
                conn.execute(
                    select(artifacts).where(
                        artifacts.c.task_id == task_id,
                        (
                            artifacts.c.intent_id.in_(related_intent_ids)
                            | artifacts.c.id.in_(artifact_ids or [""])
                        ),
                        artifacts.c.archived.is_(False),
                    )
                ).mappings()
            )
            vulnerability_rows = list(
                conn.execute(
                    select(vulnerabilities).where(
                        vulnerabilities.c.task_id == task_id,
                        (
                            vulnerabilities.c.intent_id.in_(related_intent_ids)
                            | vulnerabilities.c.id.in_(vulnerability_ids or [""])
                        ),
                        vulnerabilities.c.archived.is_(False),
                    )
                ).mappings()
            )
            knowledge_rows = self._rows(
                conn,
                task_id,
                task["project_id"],
                "knowledge",
                knowledge_ids,
            )
            return {
                "intent": intent,
                "todo": todo,
                "facts": fact_rows,
                "artifacts": [self._artifact(row) for row in artifact_rows],
                "vulnerabilities": [self._vulnerability(row) for row in vulnerability_rows],
                "knowledge": knowledge_rows,
                "hint_ids": hint_ids,
            }

    def _rows(
        self,
        conn,
        task_id: str,
        project_id: str,
        kind: str,
        ids: list[str],
    ) -> list[dict[str, Any]]:
        if not ids:
            return []
        table = ENTITY_TABLES[kind]
        if kind == "knowledge":
            statement = select(table).where(
                table.c.project_id == project_id,
                table.c.id.in_(ids),
                table.c.archived.is_(False),
            )
        else:
            statement = select(table).where(table.c.task_id == task_id, table.c.id.in_(ids))
        rows = list(conn.execute(statement).mappings())
        if kind == "intents":
            return [self._intent(conn, row) for row in rows]
        if kind == "todos":
            return [self._todo(conn, row) for row in rows]
        if kind == "artifacts":
            return [self._artifact(row) for row in rows]
        if kind == "vulnerabilities":
            return [self._vulnerability(row) for row in rows]
        return [dict(row) for row in rows]

    @staticmethod
    def _intent(conn, row) -> dict[str, Any]:
        source_ids = list(
            conn.execute(
                select(intent_sources.c.fact_id)
                .where(
                    intent_sources.c.task_id == row["task_id"],
                    intent_sources.c.intent_id == row["id"],
                )
                .order_by(intent_sources.c.fact_id)
            ).scalars()
        )
        payload = dict(row)
        payload["from_facts"] = source_ids
        payload["targets"] = json.loads(payload.pop("targets_json") or "[]")
        payload["worker_pool"] = json.loads(payload.pop("worker_pool_json") or "[]")
        return payload

    @staticmethod
    def _todo(conn, row) -> dict[str, Any]:
        sources = [
            {"type": item.source_type, "id": item.source_id}
            for item in conn.execute(
                select(todo_sources.c.source_type, todo_sources.c.source_id)
                .where(
                    todo_sources.c.task_id == row["task_id"],
                    todo_sources.c.todo_id == row["id"],
                )
                .order_by(todo_sources.c.source_type, todo_sources.c.source_id)
            )
        ]
        return {**dict(row), "sources": sources}

    @staticmethod
    def _artifact(row) -> dict[str, Any]:
        return dict(row)

    @staticmethod
    def _vulnerability(row) -> dict[str, Any]:
        payload = dict(row)
        payload["reproduction_steps"] = json.loads(payload.pop("reproduction_steps_json") or "[]")
        payload["evidence"] = json.loads(payload.pop("evidence_json") or "[]")
        return payload


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))
