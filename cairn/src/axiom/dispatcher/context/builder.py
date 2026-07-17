from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

import yaml
from pydantic import BaseModel
from sqlalchemy import Engine, func, select

from axiom.infrastructure.database.metadata import (
    artifacts,
    context_revisions,
    facts,
    intent_sources,
    intents,
    project_knowledge,
    task_hints,
    tasks,
    todo_sources,
    todos,
    vulnerabilities,
)
from axiom.modules.common.time import utcnow


class ContextSnapshot(BaseModel):
    task_id: str
    revision: int
    resources: dict[str, str]
    manifest_path: str


class ContextBuilder:
    def __init__(self, engine: Engine, *, data_dir: str | Path | None = None):
        self.engine = engine
        self.data_dir = Path(
            data_dir or os.getenv("AXIOM_DATA_DIR") or "~/.local/share/axiom"
        ).expanduser().resolve()

    def build(self, task_id: str, *, attempt_id: str) -> ContextSnapshot:
        with self.engine.begin() as conn:
            task = conn.execute(select(tasks).where(tasks.c.id == task_id)).mappings().one_or_none()
            if task is None:
                raise ValueError(f"Task not found: {task_id}")
            payloads = self._payloads(conn, task)
            resources: dict[str, str] = {}
            hashes: dict[str, str] = {}
            extensions = {"graph": "yaml", "knowledge": "md", "hints": "md"}
            for name, content in payloads.items():
                extension = extensions.get(name, "json")
                path, digest = self._write_resource(task_id, name, extension, content)
                resources[name] = str(path)
                hashes[name] = digest

            hash_json = json.dumps(hashes, ensure_ascii=False, sort_keys=True)
            latest = conn.execute(
                select(context_revisions)
                .where(context_revisions.c.task_id == task_id)
                .order_by(context_revisions.c.revision.desc())
                .limit(1)
            ).mappings().one_or_none()
            if latest is not None and latest["hash_manifest_json"] == hash_json:
                revision = latest["revision"]
            else:
                revision = int(latest["revision"] if latest is not None else 0) + 1
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

        safe_attempt = re.sub(r"[^A-Za-z0-9_.-]+", "-", attempt_id).strip("-") or "attempt"
        manifest_dir = self.data_dir / "contexts" / "manifests"
        manifest_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = manifest_dir / f"{safe_attempt}.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "task_id": task_id,
                    "revision": revision,
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

    def _payloads(self, conn, task) -> dict[str, str]:
        task_id = task["id"]
        fact_rows = conn.execute(
            select(facts).where(facts.c.task_id == task_id, facts.c.archived.is_(False))
        ).mappings().all()
        intent_rows = conn.execute(
            select(intents).where(intents.c.task_id == task_id, intents.c.archived.is_(False))
        ).mappings().all()
        graph_intents = []
        for intent in intent_rows:
            sources = conn.execute(
                select(intent_sources.c.fact_id).where(
                    intent_sources.c.task_id == task_id,
                    intent_sources.c.intent_id == intent["id"],
                )
            ).scalars().all()
            graph_intents.append(
                {
                    "id": intent["id"],
                    "todo_id": intent["todo_id"],
                    "from": list(sources),
                    "to": intent["to_fact_id"],
                    "description": intent["description"],
                    "status": intent["status"],
                    "targets": json.loads(intent["targets_json"] or "[]"),
                }
            )
        graph = {
            "task": {
                "id": task_id,
                "project_id": task["project_id"],
                "title": task["title"],
                "origin": task["origin_input"],
                "goal": task["goal"],
                "status": task["status"],
                "phase": task["phase"],
            },
            "facts": [
                {"id": row["id"], "description": row["description"]}
                for row in fact_rows
            ],
            "intents": graph_intents,
        }
        todo_rows = conn.execute(
            select(todos)
            .where(todos.c.task_id == task_id, todos.c.archived.is_(False))
            .order_by(todos.c.status, todos.c.priority.desc(), todos.c.id)
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
                    "sources": [dict(item) for item in sources],
                }
            )
        artifact_payload = [
            dict(row)
            for row in conn.execute(
                select(
                    artifacts.c.id,
                    artifacts.c.intent_id,
                    artifacts.c.artifact_type,
                    artifacts.c.path,
                    artifacts.c.summary,
                    artifacts.c.content_hash,
                ).where(artifacts.c.task_id == task_id, artifacts.c.archived.is_(False))
            ).mappings()
        ]
        vuln_payload = [
            dict(row)
            for row in conn.execute(
                select(
                    vulnerabilities.c.id,
                    vulnerabilities.c.title,
                    vulnerabilities.c.type,
                    vulnerabilities.c.severity,
                    vulnerabilities.c.status,
                    vulnerabilities.c.affected_target,
                ).where(
                    vulnerabilities.c.task_id == task_id,
                    vulnerabilities.c.archived.is_(False),
                )
            ).mappings()
        ]
        knowledge_rows = conn.execute(
            select(project_knowledge).where(
                project_knowledge.c.project_id == task["project_id"],
                project_knowledge.c.archived.is_(False),
            )
        ).mappings().all()
        hint_rows = conn.execute(
            select(task_hints).where(
                task_hints.c.task_id == task_id,
                task_hints.c.archived.is_(False),
            )
        ).mappings().all()
        return {
            "graph": yaml.safe_dump(graph, allow_unicode=True, sort_keys=False),
            "todos": json.dumps(todo_payload, ensure_ascii=False, indent=2),
            "artifacts": json.dumps(artifact_payload, ensure_ascii=False, indent=2),
            "vulnerabilities": json.dumps(vuln_payload, ensure_ascii=False, indent=2),
            "knowledge": "\n\n".join(
                f"## {row['id']} {row['title']}\n{row['content']}" for row in knowledge_rows
            ) or "No project knowledge.",
            "hints": "\n\n".join(
                f"## {row['id']}\n{row['content']}" for row in hint_rows
            ) or "No task hints.",
        }

    def _write_resource(self, task_id: str, name: str, extension: str, content: str):
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        directory = self.data_dir / "contexts" / "tasks" / task_id / name
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{digest[:16]}.{extension}"
        if not path.exists():
            path.write_text(content, encoding="utf-8")
        return path, digest
