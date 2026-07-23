from __future__ import annotations

import json
from datetime import datetime

import yaml
from sqlalchemy import Engine, select

from axiom.infrastructure.database.metadata import (
    artifacts,
    audit_events,
    facts,
    intent_sources,
    intents,
    task_hints,
    tasks,
    todo_sources,
    todos,
    vulnerabilities,
)


class TaskExportService:
    def __init__(self, engine: Engine):
        self.engine = engine

    def export(self, task_id: str, *, format: str) -> str:
        if format not in {"yaml", "timeline"}:
            raise ValueError("Supported formats: yaml, timeline")
        with self.engine.connect() as conn:
            task = conn.execute(select(tasks).where(tasks.c.id == task_id)).mappings().one_or_none()
            if task is None:
                raise ValueError(f"Task not found: {task_id}")
            payload = self._payload(conn, task)
        if format == "timeline":
            return self._timeline(payload)
        return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)

    def _payload(self, conn, task) -> dict:
        task_id = task["id"]
        fact_rows = conn.execute(
            select(facts)
            .where(facts.c.task_id == task_id, facts.c.archived.is_(False))
            .order_by(facts.c.created_at, facts.c.id)
        ).mappings().all()
        intent_rows = conn.execute(
            select(intents)
            .where(intents.c.task_id == task_id, intents.c.archived.is_(False))
            .order_by(intents.c.created_at, intents.c.id)
        ).mappings().all()
        intent_payload = []
        for row in intent_rows:
            sources = conn.execute(
                select(intent_sources.c.fact_id)
                .where(
                    intent_sources.c.task_id == task_id,
                    intent_sources.c.intent_id == row["id"],
                )
                .order_by(intent_sources.c.fact_id)
            ).scalars().all()
            intent_payload.append(
                {
                    "id": row["id"],
                    "todo_id": row["todo_id"],
                    "from": list(sources),
                    "to": row["to_fact_id"],
                    "description": row["description"],
                    "status": row["status"],
                    "creator": row["creator"],
                    "worker": row["worker"],
                    "worker_pool": json.loads(row["worker_pool_json"] or "[]"),
                    "targets": json.loads(row["targets_json"] or "[]"),
                    "created_at": _local_time(row["created_at"]),
                    "concluded_at": _local_time(row["concluded_at"]),
                }
            )
        todo_rows = conn.execute(
            select(todos)
            .where(todos.c.task_id == task_id, todos.c.archived.is_(False))
            .order_by(todos.c.created_at, todos.c.id)
        ).mappings().all()
        todo_payload = []
        for row in todo_rows:
            sources = conn.execute(
                select(todo_sources.c.source_type, todo_sources.c.source_id).where(
                    todo_sources.c.task_id == task_id,
                    todo_sources.c.todo_id == row["id"],
                )
            ).mappings().all()
            todo_payload.append(
                {
                    "id": row["id"],
                    "title": row["title"],
                    "category": row["category"],
                    "theme": row["theme"],
                    "priority": row["priority"],
                    "status": row["status"],
                    "status_reason": row["status_reason"],
                    "sources": [
                        {"type": source["source_type"], "id": source["source_id"]}
                        for source in sources
                    ],
                    "created_at": _local_time(row["created_at"]),
                    "completed_at": _local_time(row["completed_at"]),
                }
            )
        hint_payload = [
            {
                "id": row["id"],
                "content": row["content"],
                "creator": row["creator"],
                "created_at": _local_time(row["created_at"]),
            }
            for row in conn.execute(
                select(task_hints)
                .where(task_hints.c.task_id == task_id, task_hints.c.archived.is_(False))
                .order_by(task_hints.c.created_at, task_hints.c.id)
            ).mappings()
        ]
        artifact_payload = [
            {
                "id": row["id"],
                "intent_id": row["intent_id"],
                "type": row["artifact_type"],
                "path": row["path"],
                "summary": row["summary"],
                "content_hash": row["content_hash"],
                "created_at": _local_time(row["created_at"]),
            }
            for row in conn.execute(
                select(artifacts)
                .where(artifacts.c.task_id == task_id, artifacts.c.archived.is_(False))
                .order_by(artifacts.c.created_at, artifacts.c.id)
            ).mappings()
        ]
        vuln_payload = [
            {
                "id": row["id"],
                "intent_id": row["intent_id"],
                "title": row["title"],
                "type": row["type"],
                "severity": row["severity"],
                "status": row["status"],
                "status_reason": row["status_reason"],
                "affected_target": row["affected_target"],
                "created_at": _local_time(row["created_at"]),
            }
            for row in conn.execute(
                select(vulnerabilities)
                .where(
                    vulnerabilities.c.task_id == task_id,
                    vulnerabilities.c.archived.is_(False),
                )
                .order_by(vulnerabilities.c.created_at, vulnerabilities.c.id)
            ).mappings()
        ]
        event_payload = [
            {
                "id": row["id"],
                "type": row["event_type"],
                "level": row["level"],
                "worker": row["worker"],
                "intent_id": row["intent_id"],
                "message": row["message"],
                "created_at": _local_time(row["created_at"]),
            }
            for row in conn.execute(
                select(audit_events)
                .where(audit_events.c.task_id == task_id)
                .order_by(audit_events.c.created_at, audit_events.c.id)
            ).mappings()
        ]
        return {
            "task": {
                "id": task_id,
                "project_id": task["project_id"],
                "title": task["title"],
                "origin": task["origin_input"],
                "goal": task["goal"],
                "status": task["status"],
                "phase": task["phase"],
                "scope": json.loads(task["scope_json"] or "{}"),
                "worker_pool": json.loads(task["worker_pool_json"] or "[]"),
                "created_at": _local_time(task["created_at"]),
                "updated_at": _local_time(task["updated_at"]),
                "completed_at": _local_time(task["completed_at"]),
            },
            "hints": hint_payload,
            "facts": [
                {
                    "id": row["id"],
                    "description": row["description"],
                    "created_at": _local_time(row["created_at"]),
                }
                for row in fact_rows
            ],
            "todos": todo_payload,
            "intents": intent_payload,
            "vulnerabilities": vuln_payload,
            "artifacts": artifact_payload,
            "events": event_payload,
        }

    @staticmethod
    def _timeline(payload: dict) -> str:
        task = payload["task"]
        blocks = [
            f"[{task['created_at'] or ''}] TASK CREATED {task['id']}",
            f"  project: {task['project_id']}",
            f"  origin: {task['origin']}",
            f"  goal: {task['goal']}",
        ]
        sections = ["\n".join(blocks)]
        for hint in payload["hints"]:
            sections.append(
                f"[{hint['created_at'] or ''}] HINT {hint['id']} by {hint['creator']}\n"
                f"  {hint['content']}"
            )
        for event in payload["events"]:
            meta = [event["type"]]
            if event["worker"]:
                meta.append(f"worker={event['worker']}")
            if event["intent_id"]:
                meta.append(f"intent={event['intent_id']}")
            sections.append(
                f"[{event['created_at'] or ''}] {' '.join(meta)}\n  {event['message']}"
            )
        if task["completed_at"]:
            sections.append(
                f"[{task['completed_at']}] TASK COMPLETED {task['id']}\n  Goal achieved"
            )
        return "\n\n".join(sections) + "\n"


def _local_time(value: str | None) -> str | None:
    if not value:
        return value
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S")
