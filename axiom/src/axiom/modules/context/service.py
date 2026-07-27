from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import Engine, select

from axiom.modules.artifacts.repository import ArtifactRepository
from axiom.modules.context.repository import ContextRepository
from axiom.modules.context.schemas import ContextRevision, ExpandedContext
from axiom.modules.graph.repository import GraphRepository
from axiom.modules.graph.tables import facts, intents
from axiom.modules.goals.tables import goal_evaluations
from axiom.modules.knowledge.tables import project_knowledge
from axiom.modules.reason.tables import reason_runs
from axiom.modules.tasks.tables import task_hints
from axiom.modules.todos.repository import TodoRepository
from axiom.modules.vulnerabilities.repository import VulnerabilityRepository
from axiom.modules.vulnerabilities.tables import vulnerabilities
from axiom.shared.clock import utcnow
from axiom.shared.database import immediate_transaction
from axiom.shared.errors import ConflictError, NotFoundError
from axiom.shared.ids import allocate_sequence
from axiom.shared.storage import FileStore


PREVIEW_CHARS = 20


class ContextService:
    def __init__(self, engine: Engine, store: FileStore):
        self.engine = engine
        self.store = store
        self.repository = ContextRepository()
        self.graph = GraphRepository()
        self.todos = TodoRepository()
        self.artifacts = ArtifactRepository()
        self.vulnerabilities = VulnerabilityRepository()

    def build(
        self,
        *,
        task_id: str,
        phase: str,
        intent_id: str | None,
        reason_run_id: str | None = None,
        lease_id: str | None = None,
    ) -> ContextRevision:
        if intent_id is not None and reason_run_id is not None:
            raise ConflictError("Context cannot belong to both an Intent and a ReasonRun")
        if lease_id is not None and bool(intent_id) == bool(reason_run_id):
            raise ConflictError("A leased Context requires exactly one execution owner")
        prepared = None
        try:
            with immediate_transaction(self.engine) as conn:
                task = self.repository.task(conn, task_id)
                if task is None:
                    raise NotFoundError(f"Task not found: {task_id}")
                if intent_id is not None:
                    exists = conn.execute(
                        select(intents.c.id).where(
                            intents.c.task_id == task_id,
                            intents.c.id == intent_id,
                        )
                    ).scalar_one_or_none()
                    if exists is None:
                        raise NotFoundError(f"Intent not found: {task_id}/{intent_id}")
                if reason_run_id is not None:
                    exists = conn.execute(
                        select(reason_runs.c.id).where(
                            reason_runs.c.task_id == task_id,
                            reason_runs.c.id == reason_run_id,
                        )
                    ).scalar_one_or_none()
                    if exists is None:
                        raise NotFoundError(
                            f"ReasonRun not found: {task_id}/{reason_run_id}"
                        )

                if lease_id is not None:
                    existing = self.repository.for_lease(
                        conn,
                        task_id=task_id,
                        intent_id=intent_id,
                        reason_run_id=reason_run_id,
                        lease_id=lease_id,
                    )
                    if existing is not None:
                        return self._revision(existing)

                revision = allocate_sequence(conn, scope=task_id, kind="context_revision")
                base_key = f"contexts/{task_id}/r{revision:06d}"
                files = self._index_files(conn, task)
                manifest_key = f"{base_key}/manifest.json"
                manifest = {
                    "task_id": task_id,
                    "revision": revision,
                    "phase": phase,
                    "intent_id": intent_id,
                    "reason_run_id": reason_run_id,
                    "lease_id": lease_id,
                    "goal": task["goal"],
                    "origin": task["origin_input"],
                    "indexes": {
                        "graph": f"{base_key}/graph.json",
                        "todos": f"{base_key}/todos.json",
                        "artifacts": f"{base_key}/artifacts.json",
                        "hints": f"{base_key}/hints.json",
                        "vulnerabilities": f"{base_key}/vulnerabilities.json",
                        "knowledge": f"{base_key}/knowledge.json",
                        "goal_evaluations": f"{base_key}/goal_evaluations.json",
                    },
                    "detail_resolution": "将索引条目的 detail 与 manifest 所在目录拼接后读取",
                    "expansion": f"POST /api/v1/tasks/{task_id}/context/expand with refs",
                }
                files["manifest.json"] = _json(manifest)
                prepared = self.store.prepare_named_text_files(base_key=base_key, files=files)
                prepared.commit()
                content_hash = _manifest_hash(prepared.files)
                now = utcnow()
                self.repository.insert_revision(
                    conn,
                    {
                        "task_id": task_id,
                        "revision": revision,
                        "phase": phase,
                        "intent_id": intent_id,
                        "reason_run_id": reason_run_id,
                        "lease_id": lease_id,
                        "manifest_key": manifest_key,
                        "content_hash": content_hash,
                        "created_at": now,
                    },
                )
            prepared.finish()
            return ContextRevision(
                task_id=task_id,
                revision=revision,
                phase=phase,
                intent_id=intent_id,
                reason_run_id=reason_run_id,
                lease_id=lease_id,
                manifest_key=manifest_key,
                manifest_path=str(self.store.resolve(manifest_key)),
                content_hash=content_hash,
                created_at=now,
            )
        except BaseException:
            if prepared is not None:
                prepared.rollback()
            raise

    def expand(self, task_id: str, refs: list[str]) -> ExpandedContext:
        entries: dict[str, Any] = {}
        with self.engine.connect() as conn:
            if self.repository.task(conn, task_id) is None:
                raise NotFoundError(f"Task not found: {task_id}")
            for ref in refs:
                kind, separator, item_id = ref.partition(":")
                if not separator or not item_id:
                    raise ConflictError(f"Invalid context ref: {ref}")
                entries[ref] = self._expand_one(conn, task_id, kind, item_id)
        return ExpandedContext(task_id=task_id, entries=entries)

    def _index_files(self, conn, task) -> dict[str, str]:
        files: dict[str, str] = {}
        fact_rows = self.repository.facts(conn, task["id"])
        intent_rows = self.repository.intents(conn, task["id"])
        todo_rows = self.repository.todos(conn, task["id"])
        artifact_rows = self.repository.artifacts(conn, task["id"])
        hint_rows = self.repository.hints(conn, task["id"])
        vulnerability_rows = self.repository.vulnerabilities(conn, task["id"])
        knowledge_rows = self.repository.knowledge(conn, task["project_id"])
        goal_rows = self.repository.goal_evaluations(conn, task["id"])

        for row in fact_rows:
            self._add_detail(files, f"fact:{row['id']}", dict(row))
        for row in intent_rows:
            intent = self.graph.get_intent(conn, task["id"], row["id"])
            assert intent is not None
            self._add_detail(files, f"intent:{row['id']}", intent.model_dump(mode="json"))
        for row in todo_rows:
            todo = self.todos.get(conn, task["id"], row["id"])
            assert todo is not None
            self._add_detail(files, f"todo:{row['id']}", todo.model_dump(mode="json"))
        for row in artifact_rows:
            artifact = self.artifacts.get(conn, task["id"], row["id"])
            assert artifact is not None
            self._add_detail(
                files,
                f"artifact:{row['id']}",
                {
                    **artifact.model_dump(mode="json"),
                    "content_path": str(self.store.resolve(artifact.storage_key)),
                },
            )
        for row in hint_rows:
            self._add_detail(files, f"hint:{row['id']}", dict(row))
        for row in vulnerability_rows:
            vulnerability = self.vulnerabilities.get(conn, task["id"], row["id"])
            assert vulnerability is not None
            self._add_detail(
                files,
                f"vulnerability:{row['id']}",
                vulnerability.model_dump(mode="json"),
            )
        for row in knowledge_rows:
            self._add_detail(files, f"knowledge:{row['id']}", dict(row))
        for row in goal_rows:
            self._add_detail(files, f"goal_evaluation:{row['id']}", dict(row))

        graph = {
            "facts": [
                {
                    "id": row["id"],
                    "preview": _preview(row["description"]),
                    "ref": f"fact:{row['id']}",
                    "detail": _detail_path(f"fact:{row['id']}"),
                }
                for row in fact_rows
            ],
            "intents": [
                {
                    "id": row["id"],
                    "kind": row["kind"],
                    "status": row["status"],
                    "preview": _preview(row["description"]),
                    "ref": f"intent:{row['id']}",
                    "detail": _detail_path(f"intent:{row['id']}"),
                }
                for row in intent_rows
            ],
        }
        todo_index = [
            {
                "id": row["id"],
                "status": row["status"],
                "category": row["category"],
                "theme": row["theme"],
                "priority": row["priority"],
                "preview": _preview(row["title"], 60),
                "ref": f"todo:{row['id']}",
                "detail": _detail_path(f"todo:{row['id']}"),
            }
            for row in todo_rows
        ]
        artifact_index = [
            {
                "id": row["id"],
                "type": row["artifact_type"],
                "summary": _preview(row["summary"] or row["storage_key"], 80),
                "size": row["size"],
                "ref": f"artifact:{row['id']}",
                "detail": _detail_path(f"artifact:{row['id']}"),
            }
            for row in artifact_rows
        ]
        hint_index = [
            {
                "id": row["id"],
                "preview": _preview(row["content"], 80),
                "ref": f"hint:{row['id']}",
                "detail": _detail_path(f"hint:{row['id']}"),
            }
            for row in hint_rows
        ]
        vulnerability_index = [
            {
                "id": row["id"],
                "severity": row["severity"],
                "status": row["status"],
                "preview": _preview(row["title"], 80),
                "ref": f"vulnerability:{row['id']}",
                "detail": _detail_path(f"vulnerability:{row['id']}"),
            }
            for row in vulnerability_rows
        ]
        knowledge_index = [
            {
                "id": row["id"],
                "title": row["title"],
                "category": row["category"],
                "preview": _preview(row["content"], 80),
                "ref": f"knowledge:{row['id']}",
                "detail": _detail_path(f"knowledge:{row['id']}"),
            }
            for row in knowledge_rows
        ]
        goal_index = [
            {
                "id": row["id"],
                "achieved": bool(row["achieved"]),
                "preview": _preview(row["reason"], 80),
                "ref": f"goal_evaluation:{row['id']}",
                "detail": _detail_path(f"goal_evaluation:{row['id']}"),
            }
            for row in goal_rows
        ]
        files.update(
            {
                "graph.json": _json(graph),
                "todos.json": _json(todo_index),
                "artifacts.json": _json(artifact_index),
                "hints.json": _json(hint_index),
                "vulnerabilities.json": _json(vulnerability_index),
                "knowledge.json": _json(knowledge_index),
                "goal_evaluations.json": _json(goal_index),
            }
        )
        return files

    @staticmethod
    def _add_detail(files: dict[str, str], ref: str, value: Any) -> None:
        files[_detail_path(ref)] = _json(value)

    def _revision(self, row) -> ContextRevision:
        return ContextRevision(
            task_id=row["task_id"],
            revision=row["revision"],
            phase=row["phase"],
            intent_id=row["intent_id"],
            lease_id=row["lease_id"],
            manifest_key=row["manifest_key"],
            manifest_path=str(self.store.resolve(row["manifest_key"])),
            content_hash=row["content_hash"],
            created_at=row["created_at"],
        )

    def _expand_one(self, conn, task_id: str, kind: str, item_id: str) -> Any:
        if kind == "fact":
            row = conn.execute(
                select(facts).where(facts.c.task_id == task_id, facts.c.id == item_id)
            ).mappings().one_or_none()
            if row is not None:
                return dict(row)
        elif kind == "intent":
            intent = self.graph.get_intent(conn, task_id, item_id)
            if intent is not None:
                return intent.model_dump(mode="json")
        elif kind == "todo":
            todo = self.todos.get(conn, task_id, item_id)
            if todo is not None:
                return todo.model_dump(mode="json")
        elif kind == "artifact":
            artifact = self.artifacts.get(conn, task_id, item_id)
            if artifact is not None:
                return {
                    **artifact.model_dump(mode="json"),
                    "content": self.store.read_text(artifact.storage_key),
                }
        elif kind == "hint":
            row = conn.execute(
                select(task_hints).where(
                    task_hints.c.task_id == task_id,
                    task_hints.c.id == item_id,
                )
            ).mappings().one_or_none()
            if row is not None:
                return dict(row)
        elif kind == "vulnerability":
            vulnerability = conn.execute(
                select(vulnerabilities).where(
                    vulnerabilities.c.task_id == task_id,
                    vulnerabilities.c.id == item_id,
                    vulnerabilities.c.archived.is_(False),
                )
            ).mappings().one_or_none()
            if vulnerability is not None:
                model = self.vulnerabilities.get(conn, task_id, item_id)
                assert model is not None
                return model.model_dump(mode="json")
        elif kind == "knowledge":
            task = self.repository.task(conn, task_id)
            assert task is not None
            row = conn.execute(
                select(project_knowledge).where(
                    project_knowledge.c.project_id == task["project_id"],
                    project_knowledge.c.id == item_id,
                    project_knowledge.c.enabled.is_(True),
                    project_knowledge.c.archived.is_(False),
                )
            ).mappings().one_or_none()
            if row is not None:
                return dict(row)
        elif kind == "goal_evaluation":
            row = conn.execute(
                select(goal_evaluations).where(
                    goal_evaluations.c.task_id == task_id,
                    goal_evaluations.c.id == item_id,
                )
            ).mappings().one_or_none()
            if row is not None:
                return dict(row)
        raise NotFoundError(f"Context entry not found: {kind}:{item_id}")


def _preview(value: str, limit: int = PREVIEW_CHARS) -> str:
    normalized = " ".join(value.split())
    return normalized if len(normalized) <= limit else normalized[:limit] + "…"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def _manifest_hash(files) -> str:
    digest = hashlib.sha256()
    for item in sorted(files, key=lambda entry: entry.storage_key):
        digest.update(item.storage_key.encode())
        digest.update(item.content_hash.encode())
    return digest.hexdigest()


def _detail_path(ref: str) -> str:
    kind = ref.partition(":")[0]
    digest = hashlib.sha256(ref.encode()).hexdigest()[:24]
    return f"details/{kind}/{digest}.json"
