from __future__ import annotations

import hashlib
import json
from collections import Counter, deque
from pathlib import Path

from sqlalchemy import Engine, select, update

from axiom.modules.archives.repository import ArchiveRepository
from axiom.modules.archives.schemas import (
    ArchiveBatch,
    ArchiveMember,
    ArchivePreview,
    ArchiveRequestRootType,
    ArchivedArtifactContent,
)
from axiom.modules.archives.tables import archive_batch_members, archive_batches
from axiom.modules.artifacts.schemas import Artifact
from axiom.modules.artifacts.tables import artifacts
from axiom.modules.audit.schemas import RecordAuditEvent
from axiom.modules.audit.service import AuditService
from axiom.modules.graph.tables import facts, intent_sources, intents
from axiom.modules.reason.schemas import WorkspaceDraft
from axiom.modules.reason.tables import reason_run_sources, reason_runs, reason_workspaces
from axiom.modules.tasks.repository import TaskRepository
from axiom.modules.todos.tables import todos
from axiom.modules.vulnerabilities.tables import vulnerability_artifacts, vulnerabilities
from axiom.shared.clock import utcnow
from axiom.shared.database import immediate_transaction
from axiom.shared.errors import ConflictError, NotFoundError
from axiom.shared.ids import allocate_id
from axiom.shared.storage import FileStore


ACTIVE_INTENT_STATUSES = {"claimed", "running", "concluding"}
ACTIVE_REASON_STATUSES = {"pending", "claimed", "running", "concluding"}
RELATION_PRIORITY = {
    "related": 0,
    "descendant": 1,
    "output": 2,
    "producer": 3,
    "root": 4,
}


class ArchiveService:
    def __init__(self, engine: Engine, store: FileStore):
        self.engine = engine
        self.store = store
        self.repository = ArchiveRepository()
        self.tasks = TaskRepository()
        self.audit = AuditService(engine)

    def preview(
        self,
        task_id: str,
        root_type: ArchiveRequestRootType,
        root_id: str,
    ) -> ArchivePreview:
        with self.engine.connect() as conn:
            self._mutable_task(conn, task_id)
            return self._closure(conn, task_id, root_type, root_id)

    def create(
        self,
        task_id: str,
        *,
        root_type: ArchiveRequestRootType,
        root_id: str,
        expected_closure_hash: str | None,
        reason: str,
        actor: str,
    ) -> ArchiveBatch:
        now = utcnow()
        with immediate_transaction(self.engine) as conn:
            task = self._mutable_task(conn, task_id)
            preview = self._closure(conn, task_id, root_type, root_id)
            if expected_closure_hash and preview.closure_hash != expected_closure_hash:
                raise ConflictError("Archive closure changed; preview again before confirming")
            if preview.blockers:
                raise ConflictError("Archive is blocked: " + "; ".join(preview.blockers))

            archive_id = allocate_id(
                conn,
                scope="global",
                kind="archive_batch",
                prefix="ar",
                width=6,
            )
            conn.execute(
                archive_batches.insert().values(
                    id=archive_id,
                    task_id=task_id,
                    root_type=root_type,
                    root_id=root_id,
                    status="active",
                    reason=reason.strip(),
                    actor=actor.strip(),
                    closure_hash=preview.closure_hash,
                    created_at=now,
                    restored_at=None,
                    restored_by=None,
                )
            )
            conn.execute(
                archive_batch_members.insert(),
                [
                    {
                        "task_id": task_id,
                        "archive_id": archive_id,
                        "entity_type": member.entity_type,
                        "entity_id": member.entity_id,
                        "relation": member.relation,
                    }
                    for member in preview.members
                ],
            )
            self._set_archived(conn, task_id, preview.members, archived=True, now=now)
            cancelled_runs = self._cancel_affected_reason_runs(
                conn,
                task_id,
                preview.members,
                now=now,
            )
            self.audit.record_in(
                conn,
                RecordAuditEvent(
                    project_id=task.project_id,
                    task_id=task_id,
                    event_type="archive.batch.created",
                    message=f"创建级联归档批次 {archive_id}",
                    worker=actor.strip(),
                    payload={
                        "archive_id": archive_id,
                        "root_type": root_type,
                        "root_id": root_id,
                        "counts": preview.counts,
                        "reason": reason.strip(),
                        "cancelled_reason_run_ids": cancelled_runs,
                    },
                ),
            )
            row = self.repository.batch(conn, task_id, archive_id)
            assert row is not None
            return self._batch(conn, row)

    def create_compat(
        self,
        task_id: str,
        *,
        root_type: ArchiveRequestRootType,
        root_id: str,
        actor: str = "human.frontend",
    ) -> ArchiveBatch:
        preview = self.preview(task_id, root_type, root_id)
        return self.create(
            task_id,
            root_type=root_type,
            root_id=root_id,
            expected_closure_hash=preview.closure_hash,
            reason="",
            actor=actor,
        )

    def list(self, task_id: str, status: str | None = None) -> list[ArchiveBatch]:
        if status not in {None, "active", "restored"}:
            raise ConflictError("Archive status must be active or restored")
        with self.engine.connect() as conn:
            self._require_task(conn, task_id)
            return [self._batch(conn, row) for row in self.repository.list_batches(conn, task_id, status)]

    def get(self, task_id: str, archive_id: str) -> ArchiveBatch:
        with self.engine.connect() as conn:
            self._require_task(conn, task_id)
            row = self.repository.batch(conn, task_id, archive_id)
            if row is None:
                raise NotFoundError(f"Archive batch not found: {archive_id}")
            return self._batch(conn, row)

    def restore(self, task_id: str, archive_id: str, *, actor: str) -> ArchiveBatch:
        now = utcnow()
        with immediate_transaction(self.engine) as conn:
            task = self._mutable_task(conn, task_id)
            row = self.repository.batch(conn, task_id, archive_id)
            if row is None:
                raise NotFoundError(f"Archive batch not found: {archive_id}")
            if row["status"] != "active":
                raise ConflictError(f"Archive batch is already restored: {archive_id}")
            member_rows = self.repository.members(conn, task_id, archive_id)
            other_covered = self._other_active_covered(conn, task_id, archive_id)
            restored_members = [
                ArchiveMember(
                    entity_type=item["entity_type"],
                    entity_id=item["entity_id"],
                    relation=item["relation"],
                )
                for item in member_rows
                if (item["entity_type"], item["entity_id"]) not in other_covered
            ]
            self._set_archived(conn, task_id, restored_members, archived=False, now=now)
            conn.execute(
                update(archive_batches)
                .where(archive_batches.c.id == archive_id)
                .values(status="restored", restored_at=now, restored_by=actor.strip())
            )
            self.audit.record_in(
                conn,
                RecordAuditEvent(
                    project_id=task.project_id,
                    task_id=task_id,
                    event_type="archive.batch.restored",
                    message=f"恢复级联归档批次 {archive_id}",
                    worker=actor.strip(),
                    payload={
                        "archive_id": archive_id,
                        "restored_count": len(restored_members),
                        "retained_archived_count": len(member_rows) - len(restored_members),
                    },
                ),
            )
            updated = self.repository.batch(conn, task_id, archive_id)
            assert updated is not None
            result = self._batch(conn, updated)
            result.restored_count = len(restored_members)
            result.retained_archived_count = len(member_rows) - len(restored_members)
            return result

    def restore_root(
        self,
        task_id: str,
        *,
        root_type: ArchiveRequestRootType,
        root_id: str,
        actor: str = "human.frontend",
    ) -> ArchiveBatch:
        with self.engine.connect() as conn:
            self._mutable_task(conn, task_id)
            rows = conn.execute(
                select(archive_batches.c.id).where(
                    archive_batches.c.task_id == task_id,
                    archive_batches.c.root_type == root_type,
                    archive_batches.c.root_id == root_id,
                    archive_batches.c.status == "active",
                )
            ).scalars().all()
        if not rows:
            raise ConflictError(f"No active root archive for {root_type}:{root_id}")
        if len(rows) != 1:
            raise ConflictError("Multiple archive batches cover this root; restore from Archive view")
        return self.restore(task_id, rows[0], actor=actor)

    def artifact_content(
        self,
        task_id: str,
        archive_id: str,
        artifact_id: str,
    ) -> ArchivedArtifactContent:
        artifact = self._archived_artifact(task_id, archive_id, artifact_id)
        return ArchivedArtifactContent(
            artifact=artifact,
            content=self.store.read_text(artifact.storage_key),
        )

    def artifact_path(self, task_id: str, archive_id: str, artifact_id: str) -> tuple[Artifact, Path]:
        artifact = self._archived_artifact(task_id, archive_id, artifact_id)
        path = self.store.resolve(artifact.storage_key)
        if not path.is_file():
            raise NotFoundError(f"Artifact file not found: {artifact.storage_key}")
        return artifact, path

    def _archived_artifact(self, task_id: str, archive_id: str, artifact_id: str) -> Artifact:
        with self.engine.connect() as conn:
            self._require_task(conn, task_id)
            if self.repository.batch(conn, task_id, archive_id) is None:
                raise NotFoundError(f"Archive batch not found: {archive_id}")
            member = conn.execute(
                select(archive_batch_members.c.entity_id).where(
                    archive_batch_members.c.task_id == task_id,
                    archive_batch_members.c.archive_id == archive_id,
                    archive_batch_members.c.entity_type == "artifact",
                    archive_batch_members.c.entity_id == artifact_id,
                )
            ).scalar_one_or_none()
            if member is None:
                raise NotFoundError(f"Artifact is not in archive batch: {artifact_id}")
            row = conn.execute(
                select(artifacts).where(
                    artifacts.c.task_id == task_id,
                    artifacts.c.id == artifact_id,
                )
            ).mappings().one_or_none()
        if row is None:
            raise NotFoundError(f"Artifact not found: {artifact_id}")
        return Artifact.model_validate(row)

    def _closure(
        self,
        conn,
        task_id: str,
        root_type: ArchiveRequestRootType,
        root_id: str,
    ) -> ArchivePreview:
        members: dict[tuple[str, str], str] = {}
        fact_queue: deque[str] = deque()
        intent_queue: deque[str] = deque()
        seen_facts: set[str] = set()
        seen_intents: set[str] = set()
        blockers: list[str] = []

        def add(entity_type: str, entity_id: str, relation: str) -> bool:
            key = (entity_type, entity_id)
            current = members.get(key)
            if current is None or RELATION_PRIORITY[relation] > RELATION_PRIORITY[current]:
                members[key] = relation
            return current is None

        if root_type == "fact":
            root = conn.execute(
                select(facts).where(facts.c.task_id == task_id, facts.c.id == root_id)
            ).mappings().one_or_none()
            if root is None:
                raise NotFoundError(f"Fact not found: {root_id}")
            add("fact", root_id, "root")
            fact_queue.append(root_id)
            if root_id in {"origin", "goal"}:
                blockers.append(f"Protected Fact cannot be archived: {root_id}")
            producers = conn.execute(
                select(intents.c.id).where(
                    intents.c.task_id == task_id,
                    intents.c.to_fact_id == root_id,
                )
            ).scalars().all()
            for intent_id in producers:
                add("intent", intent_id, "producer")
                intent_queue.append(intent_id)
        elif root_type == "intent":
            root = conn.execute(
                select(intents).where(intents.c.task_id == task_id, intents.c.id == root_id)
            ).mappings().one_or_none()
            if root is None:
                raise NotFoundError(f"Intent not found: {root_id}")
            add("intent", root_id, "root")
            intent_queue.append(root_id)
        elif root_type == "artifact":
            root = conn.execute(
                select(artifacts.c.id).where(
                    artifacts.c.task_id == task_id,
                    artifacts.c.id == root_id,
                )
            ).scalar_one_or_none()
            if root is None:
                raise NotFoundError(f"Artifact not found: {root_id}")
            add("artifact", root_id, "root")
        else:
            root = conn.execute(
                select(vulnerabilities.c.id).where(
                    vulnerabilities.c.task_id == task_id,
                    vulnerabilities.c.id == root_id,
                )
            ).scalar_one_or_none()
            if root is None:
                raise NotFoundError(f"Vulnerability not found: {root_id}")
            add("vulnerability", root_id, "root")

        while fact_queue or intent_queue:
            while intent_queue:
                intent_id = intent_queue.popleft()
                if intent_id in seen_intents:
                    continue
                seen_intents.add(intent_id)
                row = conn.execute(
                    select(intents).where(
                        intents.c.task_id == task_id,
                        intents.c.id == intent_id,
                    )
                ).mappings().one()
                if row["kind"] == "bootstrap":
                    blockers.append(f"Bootstrap Intent cannot be archived: {intent_id}")
                if row["status"] in ACTIVE_INTENT_STATUSES:
                    blockers.append(f"Active Intent must finish or be stopped first: {intent_id}")
                if row["to_fact_id"]:
                    if add("fact", row["to_fact_id"], "output"):
                        fact_queue.append(row["to_fact_id"])

            while fact_queue:
                fact_id = fact_queue.popleft()
                if fact_id in seen_facts:
                    continue
                seen_facts.add(fact_id)
                consumers = conn.execute(
                    select(intent_sources.c.intent_id).where(
                        intent_sources.c.task_id == task_id,
                        intent_sources.c.fact_id == fact_id,
                    )
                ).scalars().all()
                for intent_id in consumers:
                    if add("intent", intent_id, "descendant"):
                        intent_queue.append(intent_id)

        lineage_intent_ids = sorted(entity_id for (kind, entity_id) in members if kind == "intent")
        if lineage_intent_ids:
            for row in conn.execute(
                select(artifacts.c.id).where(
                    artifacts.c.task_id == task_id,
                    artifacts.c.intent_id.in_(lineage_intent_ids),
                )
            ).mappings():
                add("artifact", row["id"], "related")
            vulnerability_ids = list(
                conn.execute(
                    select(vulnerabilities.c.id).where(
                        vulnerabilities.c.task_id == task_id,
                        vulnerabilities.c.intent_id.in_(lineage_intent_ids),
                    )
                ).scalars()
            )
            for vulnerability_id in vulnerability_ids:
                add("vulnerability", vulnerability_id, "related")
            if vulnerability_ids:
                for artifact_id in conn.execute(
                    select(vulnerability_artifacts.c.artifact_id).where(
                        vulnerability_artifacts.c.task_id == task_id,
                        vulnerability_artifacts.c.vulnerability_id.in_(vulnerability_ids),
                    )
                ).scalars():
                    add("artifact", artifact_id, "related")

            todo_ids = set(
                conn.execute(
                    select(intents.c.todo_id).where(
                        intents.c.task_id == task_id,
                        intents.c.id.in_(lineage_intent_ids),
                        intents.c.todo_id.is_not(None),
                    )
                ).scalars()
            )
            for todo_id in sorted(todo_ids):
                visible_outside = conn.execute(
                    select(intents.c.id)
                    .where(
                        intents.c.task_id == task_id,
                        intents.c.todo_id == todo_id,
                        intents.c.archived.is_(False),
                        intents.c.id.not_in(lineage_intent_ids),
                    )
                    .limit(1)
                ).scalar_one_or_none()
                if visible_outside is None:
                    add("todo", todo_id, "related")

        member_models = [
            self._member(conn, task_id, entity_type, entity_id, relation)
            for (entity_type, entity_id), relation in sorted(members.items())
        ]
        hash_input = [
            {
                "entity_type": item.entity_type,
                "entity_id": item.entity_id,
                "relation": item.relation,
                "status": item.status,
                "archived": item.archived,
            }
            for item in member_models
        ]
        closure_hash = hashlib.sha256(
            json.dumps(hash_input, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        return ArchivePreview(
            task_id=task_id,
            root_type=root_type,
            root_id=root_id,
            closure_hash=closure_hash,
            counts=dict(Counter(item.entity_type for item in member_models)),
            members=member_models,
            blockers=list(dict.fromkeys(blockers)),
        )

    def _batch(self, conn, row) -> ArchiveBatch:
        members = [
            self._member(
                conn,
                row["task_id"],
                item["entity_type"],
                item["entity_id"],
                item["relation"],
            )
            for item in self.repository.members(conn, row["task_id"], row["id"])
        ]
        result = ArchiveBatch(
            id=row["id"],
            task_id=row["task_id"],
            root_type=row["root_type"],
            root_id=row["root_id"],
            status=row["status"],
            reason=row["reason"],
            actor=row["actor"],
            closure_hash=row["closure_hash"],
            created_at=row["created_at"],
            restored_at=row["restored_at"],
            restored_by=row["restored_by"],
            counts=dict(Counter(item.entity_type for item in members)),
            members=members,
            blockers=[],
        )
        if row["status"] == "active":
            other_covered = self._other_active_covered(conn, row["task_id"], row["id"])
            result.restored_count = sum(
                (item.entity_type, item.entity_id) not in other_covered for item in members
            )
            result.retained_archived_count = len(members) - result.restored_count
        return result

    @staticmethod
    def _other_active_covered(conn, task_id: str, archive_id: str) -> set[tuple[str, str]]:
        return {
            (item["entity_type"], item["entity_id"])
            for item in conn.execute(
                select(
                    archive_batch_members.c.entity_type,
                    archive_batch_members.c.entity_id,
                )
                .join(
                    archive_batches,
                    archive_batches.c.id == archive_batch_members.c.archive_id,
                )
                .where(
                    archive_batch_members.c.task_id == task_id,
                    archive_batch_members.c.archive_id != archive_id,
                    archive_batches.c.status == "active",
                )
            ).mappings()
        }

    @staticmethod
    def _member(conn, task_id: str, entity_type: str, entity_id: str, relation: str) -> ArchiveMember:
        table_map = {
            "fact": (facts, facts.c.description, None),
            "intent": (intents, intents.c.description, intents.c.status),
            "artifact": (artifacts, artifacts.c.summary, None),
            "vulnerability": (vulnerabilities, vulnerabilities.c.title, vulnerabilities.c.status),
            "todo": (todos, todos.c.title, todos.c.status),
        }
        table, title_column, status_column = table_map[entity_type]
        columns = [title_column.label("title"), table.c.archived]
        if status_column is not None:
            columns.append(status_column.label("status"))
        row = conn.execute(
            select(*columns).where(table.c.task_id == task_id, table.c.id == entity_id)
        ).mappings().one_or_none()
        if row is None:
            return ArchiveMember(
                entity_type=entity_type,
                entity_id=entity_id,
                relation=relation,
                title="[记录已不存在]",
                archived=True,
            )
        return ArchiveMember(
            entity_type=entity_type,
            entity_id=entity_id,
            relation=relation,
            title=(row["title"] or "").strip(),
            status=(row.get("status") or "").strip(),
            archived=bool(row["archived"]),
        )

    @staticmethod
    def _set_archived(conn, task_id: str, members: list[ArchiveMember], *, archived: bool, now: str) -> None:
        grouped: dict[str, list[str]] = {}
        for member in members:
            grouped.setdefault(member.entity_type, []).append(member.entity_id)
        table_map = {
            "fact": facts,
            "intent": intents,
            "artifact": artifacts,
            "vulnerability": vulnerabilities,
            "todo": todos,
        }
        for entity_type, ids in grouped.items():
            values: dict[str, object] = {"archived": archived}
            if entity_type == "vulnerability":
                values["updated_at"] = now
            conn.execute(
                update(table_map[entity_type])
                .where(
                    table_map[entity_type].c.task_id == task_id,
                    table_map[entity_type].c.id.in_(ids),
                )
                .values(**values)
            )

    def _cancel_affected_reason_runs(
        self,
        conn,
        task_id: str,
        members: list[ArchiveMember],
        *,
        now: str,
    ) -> list[str]:
        archived = {(item.entity_type, item.entity_id) for item in members}
        active_rows = conn.execute(
            select(reason_runs).where(
                reason_runs.c.task_id == task_id,
                reason_runs.c.status.in_(ACTIVE_REASON_STATUSES),
            )
        ).mappings().all()
        affected: list[str] = []
        for run in active_rows:
            source_ids = set(
                conn.execute(
                    select(reason_run_sources.c.fact_id).where(
                        reason_run_sources.c.task_id == task_id,
                        reason_run_sources.c.reason_run_id == run["id"],
                    )
                ).scalars()
            )
            intersects = any(("fact", fact_id) in archived for fact_id in source_ids)
            workspace_rows = conn.execute(
                select(reason_workspaces.c.draft_json).where(
                    reason_workspaces.c.task_id == task_id,
                    reason_workspaces.c.reason_run_id == run["id"],
                    reason_workspaces.c.status == "open",
                )
            ).scalars().all()
            if not intersects:
                for raw in workspace_rows:
                    try:
                        draft = WorkspaceDraft.model_validate_json(raw or "{}")
                    except ValueError:
                        continue
                    if self._draft_refs(draft) & archived:
                        intersects = True
                        break
            if not intersects:
                continue
            affected.append(run["id"])
            conn.execute(
                update(reason_runs)
                .where(reason_runs.c.id == run["id"])
                .values(
                    status="cancelled",
                    worker=None,
                    lease_id=None,
                    claimed_at=None,
                    last_heartbeat_at=None,
                    next_retry_at=None,
                    last_failure_kind="archive_context_invalidated",
                    failure_class="",
                    activity_kind="cancelled",
                    activity_summary="归档操作使当前 Reason 上下文失效",
                    activity_tool="",
                    activity_updated_at=now,
                    updated_at=now,
                    concluded_at=now,
                )
            )
            conn.execute(
                update(reason_workspaces)
                .where(
                    reason_workspaces.c.task_id == task_id,
                    reason_workspaces.c.reason_run_id == run["id"],
                    reason_workspaces.c.status == "open",
                )
                .values(
                    status="abandoned",
                    failure_reason="archive_context_invalidated",
                    updated_at=now,
                )
            )
        return affected

    @staticmethod
    def _draft_refs(draft: WorkspaceDraft) -> set[tuple[str, str]]:
        refs: set[tuple[str, str]] = set()
        for item in draft.todo_adds:
            refs.update((source.type, source.id) for source in item.sources)
        for item in draft.todo_updates:
            refs.add(("todo", item.todo_id))
            refs.update((source.type, source.id) for source in item.source_adds)
        for item in draft.intent_proposals:
            refs.update(("fact", fact_id) for fact_id in item.from_facts)
            if item.todo_id:
                refs.add(("todo", item.todo_id))
        if draft.goal_assessment is not None:
            for ref in draft.goal_assessment.evidence_refs:
                kind, separator, item_id = ref.partition(":")
                if separator and kind in {"fact", "artifact", "vulnerability", "todo"}:
                    refs.add((kind, item_id))
        return refs

    def _require_task(self, conn, task_id: str):
        task = self.tasks.get(conn, task_id)
        if task is None:
            raise NotFoundError(f"Task not found: {task_id}")
        return task

    def _mutable_task(self, conn, task_id: str):
        task = self._require_task(conn, task_id)
        if task.legacy_readonly:
            raise ConflictError("Legacy Task is read-only")
        return task
