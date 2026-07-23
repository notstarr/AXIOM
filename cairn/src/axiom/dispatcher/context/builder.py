from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel
from sqlalchemy import Engine, and_, case, func, or_, select

from axiom.dispatcher.context.reader import ContextReader
from axiom.infrastructure.database.metadata import (
    artifacts,
    context_revisions,
    facts,
    intent_sources,
    intents,
    project_knowledge,
    reason_workspaces,
    task_hints,
    tasks,
    todo_sources,
    todos,
    vulnerabilities,
)
from axiom.modules.common.time import utcnow
from axiom.modules.common.ids import allocate_sequence_value


ContextPhase = Literal["bootstrap", "reason", "explore", "generic"]
DEFAULT_PREVIEW_CHARS = 60
DELTA_LIMITS = {
    "facts": 20,
    "intents": 20,
    "todos": 30,
    "artifacts": 40,
    "vulnerabilities": 20,
}


class ContextSnapshot(BaseModel):
    task_id: str
    revision: int
    resources: dict[str, str]
    manifest_path: str


class ContextBuilder:
    def __init__(
        self,
        engine: Engine,
        *,
        data_dir: str | Path | None = None,
        preview_chars: int | None = None,
    ):
        self.engine = engine
        self.reader = ContextReader(engine)
        self.data_dir = Path(
            data_dir or os.getenv("AXIOM_DATA_DIR") or "~/.local/share/axiom"
        ).expanduser().resolve()
        configured_preview = preview_chars
        if configured_preview is None:
            configured_preview = int(
                os.getenv("AXIOM_CONTEXT_PREVIEW_CHARS", str(DEFAULT_PREVIEW_CHARS))
            )
        self.preview_chars = max(20, min(int(configured_preview), 200))

    def build(
        self,
        task_id: str,
        *,
        attempt_id: str,
        phase: ContextPhase = "generic",
        intent_id: str | None = None,
    ) -> ContextSnapshot:
        if phase in {"bootstrap", "explore"} and not intent_id:
            raise ValueError(f"intent_id is required for {phase} context")
        with self.engine.begin() as conn:
            task = conn.execute(select(tasks).where(tasks.c.id == task_id)).mappings().one_or_none()
            if task is None:
                raise ValueError(f"Task not found: {task_id}")

            base_payloads, source_state_hash = self._base_payloads(conn, task)
            resources: dict[str, str] = {}
            hashes: dict[str, str] = {}
            extensions = {
                "graph": "yaml",
                "knowledge": "json",
                "hints": "md",
            }
            for name, content in base_payloads.items():
                extension = extensions.get(name, "json")
                path, digest = self._write_resource(task_id, name, extension, content)
                resources[name] = str(path)
                hashes[name] = digest

            hash_json = json.dumps(
                {**hashes, "_source_state": source_state_hash},
                ensure_ascii=False,
                sort_keys=True,
            )
            latest = conn.execute(
                select(context_revisions)
                .where(context_revisions.c.task_id == task_id)
                .order_by(context_revisions.c.revision.desc())
                .limit(1)
            ).mappings().one_or_none()
            if latest is not None and latest["hash_manifest_json"] == hash_json:
                revision = latest["revision"]
            else:
                revision = allocate_sequence_value(
                    conn,
                    scope=task_id,
                    kind="context_revision",
                    minimum=int(latest["revision"] if latest is not None else 0) + 1,
                )
                conn.execute(
                    context_revisions.insert().values(
                        task_id=task_id,
                        revision=revision,
                        graph_path=resources["graph"],
                        todo_path=resources["todos"],
                        artifact_index_path=resources["artifacts"],
                        hints_path=resources["hints"],
                        knowledge_path=resources["knowledge"],
                        hash_manifest_json=hash_json,
                        created_at=utcnow(),
                    )
                )

            phase_payload = self._phase_context(
                conn,
                task,
                phase=phase,
                intent_id=intent_id,
            )

        phase_content = json.dumps(phase_payload, ensure_ascii=False, indent=2)
        phase_path, phase_hash = self._write_attempt_resource(
            task_id,
            attempt_id,
            "phase_context.json",
            phase_content,
        )
        resources["phase_context"] = str(phase_path)
        hashes["phase_context"] = phase_hash

        safe_attempt = _safe_name(attempt_id)
        manifest_dir = self.data_dir / "contexts" / "manifests"
        manifest_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = manifest_dir / f"{safe_attempt}.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "task_id": task_id,
                    "revision": revision,
                    "phase": phase,
                    "intent_id": intent_id,
                    "progressive_disclosure": {
                        "preview_chars": self.preview_chars,
                        "policy": "Read phase_context first; use compact indexes for navigation and expand full entities only when needed.",
                        "get_command": "axiom-tool context get --fact ID --intent ID --todo ID --knowledge ID --artifact ID --vulnerability ID",
                        "related_command": "axiom-tool context related --intent ID",
                    },
                    "resources": resources,
                    "hashes": hashes,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return ContextSnapshot(
            task_id=task_id,
            revision=revision,
            resources=resources,
            manifest_path=str(manifest_path),
        )

    def _base_payloads(self, conn, task) -> tuple[dict[str, str], str]:
        task_id = task["id"]
        fact_rows = list(
            conn.execute(
                select(facts)
                .where(facts.c.task_id == task_id, facts.c.archived.is_(False))
                .order_by(facts.c.created_at, facts.c.id)
            ).mappings()
        )
        intent_rows = list(
            conn.execute(
                select(intents)
                .where(intents.c.task_id == task_id, intents.c.archived.is_(False))
                .order_by(intents.c.created_at, intents.c.id)
            ).mappings()
        )
        graph_intents = []
        for intent in intent_rows:
            sources = list(
                conn.execute(
                    select(intent_sources.c.fact_id)
                    .where(
                        intent_sources.c.task_id == task_id,
                        intent_sources.c.intent_id == intent["id"],
                    )
                    .order_by(intent_sources.c.fact_id)
                ).scalars()
            )
            graph_intents.append(
                {
                    "id": intent["id"],
                    "todo_id": intent["todo_id"],
                    "from": sources,
                    "to": intent["to_fact_id"],
                    "description": self._preview(intent["description"]),
                    "description_truncated": len(_compact(intent["description"])) > self.preview_chars,
                    "status": intent["status"],
                    "targets": json.loads(intent["targets_json"] or "[]"),
                    "retry_count": intent["retry_count"],
                    "last_failure_kind": intent["last_failure_kind"],
                }
            )
        graph = {
            "task": self._task_payload(task),
            "index_policy": {
                "description_preview_chars": self.preview_chars,
                "full_details": "Use axiom-tool context get with the Fact or Intent ID.",
            },
            "facts": [
                {
                    "id": row["id"],
                    "description": self._preview(row["description"]),
                    "description_truncated": len(_compact(row["description"])) > self.preview_chars,
                    "created_at": row["created_at"],
                }
                for row in fact_rows
            ],
            "intents": graph_intents,
        }

        todo_rows = list(
            conn.execute(
                select(todos)
                .where(todos.c.task_id == task_id, todos.c.archived.is_(False))
                .order_by(todos.c.status, todos.c.priority.desc(), todos.c.id)
            ).mappings()
        )
        active_todos: list[dict[str, Any]] = []
        historical_todos: list[dict[str, Any]] = []
        for row in todo_rows:
            sources = self._todo_sources(conn, task_id, row["id"])
            payload = {
                "id": row["id"],
                "title": row["title"] if row["status"] in {"pending", "active"} else self._preview(row["title"]),
                "category": row["category"],
                "theme": row["theme"],
                "priority": row["priority"],
                "status": row["status"],
                "status_reason": row["status_reason"] if row["status"] in {"pending", "active"} else self._preview(row["status_reason"]),
                "sources": sources,
                "updated_at": row["updated_at"],
            }
            (active_todos if row["status"] in {"pending", "active"} else historical_todos).append(payload)

        artifact_rows = list(
            conn.execute(
                select(
                    artifacts.c.id,
                    artifacts.c.intent_id,
                    artifacts.c.artifact_type,
                    artifacts.c.path,
                    artifacts.c.summary,
                    artifacts.c.content_hash,
                    artifacts.c.size,
                    artifacts.c.created_at,
                )
                .where(artifacts.c.task_id == task_id, artifacts.c.archived.is_(False))
                .order_by(artifacts.c.created_at, artifacts.c.id)
            ).mappings()
        )
        artifact_payload = [
            {
                **dict(row),
                "summary": self._preview(row["summary"], limit=max(self.preview_chars, 120)),
            }
            for row in artifact_rows
        ]
        vuln_payload = [
            dict(row)
            for row in conn.execute(
                select(
                    vulnerabilities.c.id,
                    vulnerabilities.c.intent_id,
                    vulnerabilities.c.title,
                    vulnerabilities.c.type,
                    vulnerabilities.c.severity,
                    vulnerabilities.c.status,
                    vulnerabilities.c.affected_target,
                    vulnerabilities.c.updated_at,
                )
                .where(
                    vulnerabilities.c.task_id == task_id,
                    vulnerabilities.c.archived.is_(False),
                )
                .order_by(vulnerabilities.c.created_at, vulnerabilities.c.id)
            ).mappings()
        ]
        knowledge_rows = list(
            conn.execute(
                select(project_knowledge)
                .where(
                    project_knowledge.c.project_id == task["project_id"],
                    project_knowledge.c.archived.is_(False),
                )
                .order_by(project_knowledge.c.updated_at, project_knowledge.c.id)
            ).mappings()
        )
        knowledge_payload = [
            {
                "id": row["id"],
                "title": row["title"],
                "hint": row["hint"],
                "content_preview": self._preview(row["content"]),
                "updated_at": row["updated_at"],
            }
            for row in knowledge_rows
        ]
        hint_rows = list(
            conn.execute(
                select(task_hints)
                .where(
                    task_hints.c.task_id == task_id,
                    task_hints.c.archived.is_(False),
                )
                .order_by(task_hints.c.created_at, task_hints.c.id)
            ).mappings()
        )
        payloads = {
            "graph": yaml.safe_dump(graph, allow_unicode=True, sort_keys=False),
            "todos": json.dumps(
                {"active": active_todos, "history": historical_todos},
                ensure_ascii=False,
                indent=2,
            ),
            "artifacts": json.dumps(artifact_payload, ensure_ascii=False, indent=2),
            "vulnerabilities": json.dumps(vuln_payload, ensure_ascii=False, indent=2),
            "knowledge": json.dumps(knowledge_payload, ensure_ascii=False, indent=2),
            "hints": "\n\n".join(
                f"## {row['id']}\n{row['content']}" for row in hint_rows
            ) or "No task hints.",
        }
        source_state_hash = _digest(
            json.dumps(
                {
                    "facts": [(row["id"], row["description"]) for row in fact_rows],
                    "intents": [
                        (
                            row["id"],
                            row["description"],
                            row["status"],
                            row["targets_json"],
                            row["to_fact_id"],
                        )
                        for row in intent_rows
                    ],
                    "todos": [
                        (
                            row["id"],
                            row["title"],
                            row["theme"],
                            row["status"],
                            row["status_reason"],
                            row["updated_at"],
                        )
                        for row in todo_rows
                    ],
                    "artifacts": [
                        (
                            row["id"],
                            row["summary"],
                            row["content_hash"],
                        )
                        for row in artifact_rows
                    ],
                    "vulnerabilities": [
                        (row["id"], row["title"], row["status"], row["updated_at"])
                        for row in vuln_payload
                    ],
                    "knowledge": [
                        (row["id"], row["title"], row["hint"], row["content"], row["updated_at"])
                        for row in knowledge_rows
                    ],
                    "hints": [(row["id"], row["content"]) for row in hint_rows],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return payloads, source_state_hash

    def _phase_context(self, conn, task, *, phase: ContextPhase, intent_id: str | None) -> dict[str, Any]:
        base: dict[str, Any] = {
            "phase": phase,
            "task": self._task_payload(task, full=True),
            "reading_policy": {
                "hot_context": "This file contains full details required for the current phase.",
                "global_indexes": "graph/todos/artifacts/vulnerabilities/knowledge are navigation indexes; previews are not complete evidence.",
                "expand": "Use axiom-tool context get for full entities, preferably batching multiple IDs.",
            },
        }
        if phase in {"bootstrap", "explore"} and intent_id:
            base["execution_packet"] = self.reader.related_intent(task["id"], intent_id)
            return base
        if phase == "reason":
            # The workspace must consume changes after the context snapshot
            # it was opened against. Using committed_at loses an Explore
            # result that arrives while Reason is still planning.
            cutoff = conn.execute(
                select(
                    case(
                        (
                            context_revisions.c.created_at
                            <= reason_workspaces.c.committed_at,
                            context_revisions.c.created_at,
                        ),
                        else_=reason_workspaces.c.committed_at,
                    )
                )
                .select_from(
                    reason_workspaces.join(
                        context_revisions,
                        and_(
                            context_revisions.c.task_id == reason_workspaces.c.task_id,
                            context_revisions.c.revision == reason_workspaces.c.context_revision,
                        ),
                    )
                )
                .where(
                    reason_workspaces.c.task_id == task["id"],
                    reason_workspaces.c.status == "committed",
                )
                .order_by(reason_workspaces.c.committed_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            base["active_todos"] = self._full_todos(
                conn,
                task["id"],
                statuses={"pending", "active"},
            )
            base["active_intents"] = self._full_intents(
                conn,
                task["id"],
                statuses={"pending", "claimed", "running", "concluding"},
            )
            base["recent_delta"] = self._reason_delta(conn, task, cutoff=cutoff)
        return base

    def _reason_delta(self, conn, task, *, cutoff: str | None) -> dict[str, Any]:
        task_id = task["id"]
        fact_query = select(facts).where(
            facts.c.task_id == task_id,
            facts.c.archived.is_(False),
            facts.c.id.not_in(["origin", "goal"]),
        )
        intent_query = select(intents).where(
            intents.c.task_id == task_id,
            intents.c.archived.is_(False),
        )
        todo_query = select(todos).where(todos.c.task_id == task_id, todos.c.archived.is_(False))
        artifact_query = select(artifacts).where(
            artifacts.c.task_id == task_id,
            artifacts.c.archived.is_(False),
        )
        vuln_query = select(
            vulnerabilities.c.id,
            vulnerabilities.c.intent_id,
            vulnerabilities.c.title,
            vulnerabilities.c.type,
            vulnerabilities.c.severity,
            vulnerabilities.c.status,
            vulnerabilities.c.affected_target,
            vulnerabilities.c.updated_at,
        ).where(
            vulnerabilities.c.task_id == task_id,
            vulnerabilities.c.archived.is_(False),
        )
        if cutoff:
            fact_query = fact_query.where(facts.c.created_at > cutoff)
            intent_query = intent_query.where(
                or_(intents.c.created_at > cutoff, intents.c.concluded_at > cutoff)
            )
            todo_query = todo_query.where(todos.c.updated_at > cutoff)
            artifact_query = artifact_query.where(artifacts.c.created_at > cutoff)
            vuln_query = vuln_query.where(vulnerabilities.c.updated_at > cutoff)

        fact_rows, fact_omitted = self._limited_rows(
            conn,
            fact_query,
            order_column=facts.c.created_at,
            limit=DELTA_LIMITS["facts"],
        )
        intent_rows, intent_omitted = self._limited_rows(
            conn,
            intent_query,
            order_column=intents.c.created_at,
            limit=DELTA_LIMITS["intents"],
        )
        todo_rows, todo_omitted = self._limited_rows(
            conn,
            todo_query,
            order_column=todos.c.updated_at,
            limit=DELTA_LIMITS["todos"],
        )
        artifact_rows, artifact_omitted = self._limited_rows(
            conn,
            artifact_query,
            order_column=artifacts.c.created_at,
            limit=DELTA_LIMITS["artifacts"],
        )
        vuln_rows, vuln_omitted = self._limited_rows(
            conn,
            vuln_query,
            order_column=vulnerabilities.c.updated_at,
            limit=DELTA_LIMITS["vulnerabilities"],
        )
        fact_payload = [dict(row) for row in fact_rows]
        intent_payload = [self._intent_payload(conn, row, full=True) for row in intent_rows]
        return {
            "since": cutoff,
            "new_fact_ids": [row["id"] for row in fact_payload],
            "completed_intent_ids": [
                row["id"] for row in intent_payload if row["status"] == "completed"
            ],
            "failed_intent_ids": [
                row["id"] for row in intent_payload if row["status"] == "failed"
            ],
            "facts": fact_payload,
            "intents": intent_payload,
            "todos": [self._todo_payload(conn, row) for row in todo_rows],
            "artifacts": [dict(row) for row in artifact_rows],
            "vulnerabilities": [dict(row) for row in vuln_rows],
            "omitted_counts": {
                "facts": fact_omitted,
                "intents": intent_omitted,
                "todos": todo_omitted,
                "artifacts": artifact_omitted,
                "vulnerabilities": vuln_omitted,
            },
        }

    @staticmethod
    def _limited_rows(conn, query, *, order_column, limit: int):
        total = int(
            conn.execute(
                select(func.count()).select_from(query.order_by(None).subquery())
            ).scalar_one()
        )
        selected = list(conn.execute(query.order_by(order_column.desc()).limit(limit)).mappings())
        omitted = max(0, total - len(selected))
        selected.reverse()
        return selected, omitted

    def _full_todos(self, conn, task_id: str, *, statuses: set[str]) -> list[dict[str, Any]]:
        rows = conn.execute(
            select(todos)
            .where(
                todos.c.task_id == task_id,
                todos.c.archived.is_(False),
                todos.c.status.in_(statuses),
            )
            .order_by(todos.c.priority.desc(), todos.c.updated_at, todos.c.id)
        ).mappings()
        return [self._todo_payload(conn, row) for row in rows]

    def _full_intents(self, conn, task_id: str, *, statuses: set[str]) -> list[dict[str, Any]]:
        rows = conn.execute(
            select(intents)
            .where(
                intents.c.task_id == task_id,
                intents.c.archived.is_(False),
                intents.c.status.in_(statuses),
            )
            .order_by(intents.c.created_at, intents.c.id)
        ).mappings()
        return [self._intent_payload(conn, row, full=True) for row in rows]

    def _intent_payload(self, conn, row, *, full: bool) -> dict[str, Any]:
        description = row["description"] if full else self._preview(row["description"])
        return {
            "id": row["id"],
            "todo_id": row["todo_id"],
            "from_facts": list(
                conn.execute(
                    select(intent_sources.c.fact_id)
                    .where(
                        intent_sources.c.task_id == row["task_id"],
                        intent_sources.c.intent_id == row["id"],
                    )
                    .order_by(intent_sources.c.fact_id)
                ).scalars()
            ),
            "to_fact_id": row["to_fact_id"],
            "description": description,
            "status": row["status"],
            "worker": row["worker"],
            "targets": json.loads(row["targets_json"] or "[]"),
            "retry_count": row["retry_count"],
            "last_failure_kind": row["last_failure_kind"],
            "created_at": row["created_at"],
            "concluded_at": row["concluded_at"],
        }

    def _todo_payload(self, conn, row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "title": row["title"],
            "category": row["category"],
            "theme": row["theme"],
            "priority": row["priority"],
            "status": row["status"],
            "status_reason": row["status_reason"],
            "sources": self._todo_sources(conn, row["task_id"], row["id"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "completed_at": row["completed_at"],
        }

    @staticmethod
    def _todo_sources(conn, task_id: str, todo_id: str) -> list[dict[str, str]]:
        return [
            {"type": row.source_type, "id": row.source_id}
            for row in conn.execute(
                select(todo_sources.c.source_type, todo_sources.c.source_id)
                .where(
                    todo_sources.c.task_id == task_id,
                    todo_sources.c.todo_id == todo_id,
                )
                .order_by(todo_sources.c.source_type, todo_sources.c.source_id)
            )
        ]

    @staticmethod
    def _task_payload(task, *, full: bool = False) -> dict[str, Any]:
        scope = json.loads(task["scope_json"] or "{}")
        payload = {
            "id": task["id"],
            "project_id": task["project_id"],
            "title": task["title"],
            "origin": task["origin_input"],
            "goal": task["goal"],
            "status": task["status"],
            "phase": task["phase"],
            "scope": {
                "enabled": bool(task["scope_enabled"]),
                "targets": list(scope.get("targets") or []),
                "exclude": list(scope.get("exclude") or []),
                "notes": str(scope.get("notes") or ""),
            },
        }
        if full:
            payload.update(
                {
                    "guard_enabled": bool(task["guard_enabled"]),
                    "external_report_enabled": bool(task["external_report_enabled"]),
                    "worker_pool": json.loads(task["worker_pool_json"] or "[]"),
                }
            )
        return payload

    def _preview(self, value: str, *, limit: int | None = None) -> str:
        text = _compact(value)
        size = limit or self.preview_chars
        return text if len(text) <= size else text[:size] + "…"

    def _write_resource(self, task_id: str, name: str, extension: str, content: str):
        digest = _digest(content)
        directory = self.data_dir / "contexts" / "tasks" / task_id / name
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{digest[:16]}.{extension}"
        if not path.exists():
            path.write_text(content, encoding="utf-8")
        return path, digest

    def _write_attempt_resource(self, task_id: str, attempt_id: str, filename: str, content: str):
        digest = _digest(content)
        directory = self.data_dir / "contexts" / "attempts" / task_id / _safe_name(attempt_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / filename
        path.write_text(content, encoding="utf-8")
        return path, digest


def _compact(value: str) -> str:
    return " ".join((value or "").split())


def _digest(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "attempt"
