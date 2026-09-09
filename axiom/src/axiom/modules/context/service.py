from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from sqlalchemy import Engine, select

from axiom.modules.artifacts.repository import ArtifactRepository
from axiom.modules.artifacts.tables import artifacts
from axiom.modules.audit.tables import audit_events
from axiom.modules.context.repository import ContextRepository
from axiom.modules.context.schemas import ContextRevision, ExpandedContext
from axiom.modules.context.injection import ContextInjectionService, InjectedSystemContext
from axiom.modules.graph.repository import GraphRepository
from axiom.modules.graph.tables import facts, intents
from axiom.modules.goals.tables import goal_evaluations
from axiom.modules.knowledge.tables import project_knowledge
from axiom.modules.reason.tables import reason_run_sources, reason_runs
from axiom.modules.plan.tables import plan_run_sources, plan_runs
from axiom.modules.tasks.schemas import TaskScope
from axiom.modules.vulnerability_reviews.tables import vulnerability_review_runs
from axiom.modules.tasks.tables import task_hints
from axiom.modules.todos.tables import todo_sources
from axiom.modules.todos.repository import TodoRepository
from axiom.modules.todos.tables import todos
from axiom.modules.vulnerabilities.repository import VulnerabilityRepository
from axiom.modules.vulnerabilities.tables import vulnerabilities
from axiom.shared.clock import utcnow
from axiom.shared.database import transaction
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
        self.injection = ContextInjectionService()

    def build_system_context(
        self,
        *,
        revision: ContextRevision,
        phase: str,
        intent_id: str | None = None,
        reason_run_id: str | None = None,
        plan_run_id: str | None = None,
        review_run_id: str | None = None,
        current_description: str = "",
        current_targets: list[str] | None = None,
        trigger_payload: dict[str, Any] | None = None,
    ) -> InjectedSystemContext:
        return self.injection.build(
            revision=revision,
            phase=phase,
            intent_id=intent_id,
            reason_run_id=reason_run_id,
            plan_run_id=plan_run_id,
            review_run_id=review_run_id,
            current_description=current_description,
            current_targets=current_targets,
            trigger_payload=trigger_payload,
        )

    def build(
        self,
        *,
        task_id: str,
        phase: str,
        intent_id: str | None,
        reason_run_id: str | None = None,
        plan_run_id: str | None = None,
        review_run_id: str | None = None,
        lease_id: str | None = None,
    ) -> ContextRevision:
        # Unowned revisions are useful for human previews, but a leased
        # worker revision must belong to the exact execution owner.  Keep Plan
        # ownership explicit so a stale Reason/Plan revision can never be
        # opened merely because it belongs to the same task.
        if phase == "plan" and plan_run_id is None and lease_id is not None:
            raise ConflictError("A leased Plan Context requires plan_run_id")
        if phase != "plan" and plan_run_id is not None:
            raise ConflictError("plan_run_id requires phase=plan")
        owners = [
            item
            for item in (intent_id, reason_run_id, plan_run_id, review_run_id)
            if item
        ]
        if len(owners) > 1:
            raise ConflictError("Context cannot belong to multiple execution owners")
        if lease_id is not None and len(owners) != 1:
            raise ConflictError("A leased Context requires exactly one execution owner")
        prepared = None
        try:
            with transaction(self.engine) as conn:
                task = self.repository.task(conn, task_id)
                if task is None:
                    raise NotFoundError(f"Task not found: {task_id}")
                if task.get("archived", False):
                    raise ConflictError("已归档任务不能生成 Worker Context")
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
                if plan_run_id is not None:
                    plan_row = conn.execute(
                        select(
                            plan_runs.c.id,
                            plan_runs.c.status,
                            plan_runs.c.lease_id,
                        ).where(
                            plan_runs.c.task_id == task_id,
                            plan_runs.c.id == plan_run_id,
                        )
                    ).mappings().one_or_none()
                    if plan_row is None:
                        raise NotFoundError(
                            f"PlanRun not found: {task_id}/{plan_run_id}"
                        )
                    if lease_id is not None:
                        if plan_row["lease_id"] != lease_id:
                            raise ConflictError("PlanRun lease does not match Context lease")
                        if plan_row["status"] not in {"claimed", "running", "concluding"}:
                            raise ConflictError("PlanRun lease is not active")
                if review_run_id is not None:
                    exists = conn.execute(
                        select(vulnerability_review_runs.c.id).where(
                            vulnerability_review_runs.c.task_id == task_id,
                            vulnerability_review_runs.c.id == review_run_id,
                        )
                    ).scalar_one_or_none()
                    if exists is None:
                        raise NotFoundError(
                            f"Vulnerability ReviewRun not found: {task_id}/{review_run_id}"
                        )

                if lease_id is not None:
                    existing = self.repository.for_lease(
                        conn,
                        task_id=task_id,
                        intent_id=intent_id,
                        reason_run_id=reason_run_id,
                        plan_run_id=plan_run_id,
                        review_run_id=review_run_id,
                        lease_id=lease_id,
                    )
                    if existing is not None:
                        return self._revision(existing)

                revision = allocate_sequence(conn, scope=task_id, kind="context_revision")
                base_key = f"contexts/{task_id}/r{revision:06d}"
                include_goal_history = phase not in {"reason", "review"}
                include_archived = phase == "plan"
                files = self._index_files(
                    conn,
                    task,
                    include_goal_history=include_goal_history,
                    include_archived=include_archived,
                )
                manifest_key = f"{base_key}/manifest.json"
                indexes = {
                    "graph": f"{base_key}/graph.json",
                    "todos": f"{base_key}/todos.json",
                    "artifacts": f"{base_key}/artifacts.json",
                    "hints": f"{base_key}/hints.json",
                    "vulnerabilities": f"{base_key}/vulnerabilities.json",
                    "knowledge": f"{base_key}/knowledge.json",
                }
                if review_run_id is not None:
                    review_row = conn.execute(
                        select(vulnerability_review_runs.c.vulnerability_id).where(
                            vulnerability_review_runs.c.task_id == task_id,
                            vulnerability_review_runs.c.id == review_run_id,
                        )
                    ).mappings().one()
                    target = self.vulnerabilities.get(
                        conn,
                        task_id,
                        review_row["vulnerability_id"],
                    )
                    if target is None or target.archived:
                        raise ConflictError("Review target is no longer visible")
                    target_payload = target.model_dump(mode="json")
                    if target.finding_artifact is not None:
                        target_payload["finding_artifact"]["content_path"] = str(
                            self.store.resolve(target.finding_artifact.storage_key)
                        )
                    for artifact in target_payload.get("evidence_artifacts", []):
                        artifact["content_path"] = str(
                            self.store.resolve(artifact["storage_key"])
                        )
                    files["review_target.json"] = _json(target_payload)
                    indexes["review_target"] = f"{base_key}/review_target.json"
                if include_goal_history:
                    indexes.update(
                        {
                            "goal_evaluations": f"{base_key}/goal_evaluations.json",
                            "goal_cycles": f"{base_key}/goal_cycles.json",
                        }
                    )
                manifest = {
                    "task_id": task_id,
                    "revision": revision,
                    "phase": phase,
                    "intent_id": intent_id,
                    "reason_run_id": reason_run_id,
                    "plan_run_id": plan_run_id,
                    "review_run_id": review_run_id,
                    "lease_id": lease_id,
                    "goal": task["goal"],
                    "origin": task["origin_input"],
                    "indexes": indexes,
                    "detail_resolution": "将索引条目的 detail 与 manifest 所在目录拼接后读取",
                    "expansion": f"POST /api/v1/tasks/{task_id}/context/expand with refs",
                }
                raw_scope = task.get("scope_json") or {}
                scope = TaskScope.model_validate(
                    json.loads(raw_scope) if isinstance(raw_scope, str) else raw_scope
                )
                if scope.enabled:
                    manifest["scope"] = scope.model_dump(mode="json")
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
                        "plan_run_id": plan_run_id,
                        "review_run_id": review_run_id,
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
                plan_run_id=plan_run_id,
                review_run_id=review_run_id,
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

    def expand(
        self,
        task_id: str,
        refs: list[str],
        *,
        phase: str | None = None,
    ) -> ExpandedContext:
        include_archived = phase == "plan"
        entries: dict[str, Any] = {}
        with self.engine.connect() as conn:
            task = self.repository.task(conn, task_id)
            if task is None:
                raise NotFoundError(f"Task not found: {task_id}")
            if task.get("archived", False):
                raise ConflictError("已归档任务的 Context 不可见，请先恢复任务")
            for ref in refs:
                kind, separator, item_id = ref.partition(":")
                if not separator or not item_id:
                    raise ConflictError(f"Invalid context ref: {ref}")
                entries[ref] = self._expand_one(
                    conn,
                    task_id,
                    kind,
                    item_id,
                    include_archived=include_archived,
                )
        return ExpandedContext(task_id=task_id, entries=entries)

    def _index_files(
        self,
        conn,
        task,
        *,
        include_goal_history: bool,
        include_archived: bool = False,
    ) -> dict[str, str]:
        files: dict[str, str] = {}
        fact_rows = self.repository.facts(conn, task["id"], include_archived=include_archived)
        intent_rows = self.repository.intents(conn, task["id"], include_archived=include_archived)
        todo_rows = self.repository.todos(conn, task["id"], include_archived=include_archived)
        artifact_rows = self.repository.artifacts(conn, task["id"], include_archived=include_archived)
        hint_rows = self.repository.hints(conn, task["id"], include_archived=include_archived)
        vulnerability_rows = self.repository.vulnerabilities(
            conn, task["id"], include_archived=include_archived
        )
        knowledge_rows = self.repository.knowledge(
            conn, task["project_id"], include_archived=include_archived
        )
        goal_rows = (
            self.repository.goal_evaluations(conn, task["id"])
            if include_goal_history
            else []
        )
        goal_cycle_rows = (
            self.repository.goal_cycles(conn, task["id"])
            if include_goal_history
            else []
        )
        visible_fact_ids = {row["id"] for row in fact_rows}
        visible_intent_ids = {row["id"] for row in intent_rows}
        source_intents_by_fact: dict[str, list[str]] = {}
        intent_sources_by_id: dict[str, list[str]] = {}
        intent_rows_by_id = {row["id"]: row for row in intent_rows}
        for row in intent_rows:
            intent = self.graph.get_intent(conn, task["id"], row["id"])
            if intent is None:
                continue
            intent_sources_by_id[row["id"]] = list(intent.from_facts)
            if intent.to_fact_id in visible_fact_ids:
                source_intents_by_fact.setdefault(intent.to_fact_id, []).append(row["id"])
        artifacts_by_intent: dict[str, list[str]] = {}
        for row in artifact_rows:
            if row.get("intent_id") in visible_intent_ids:
                artifacts_by_intent.setdefault(row["intent_id"], []).append(row["id"])
        fact_consumption: dict[str, list[dict[str, str]]] = {}
        consumption_rows = conn.execute(
            select(
                reason_run_sources.c.fact_id,
                reason_runs.c.id.label("reason_run_id"),
                reason_runs.c.status,
                reason_runs.c.trigger,
            )
            .join(
                reason_runs,
                (reason_runs.c.task_id == reason_run_sources.c.task_id)
                & (reason_runs.c.id == reason_run_sources.c.reason_run_id),
            )
            .where(reason_run_sources.c.task_id == task["id"])
            .order_by(reason_runs.c.created_at, reason_runs.c.id)
        ).mappings()
        for row in consumption_rows:
            fact_consumption.setdefault(row["fact_id"], []).append(
                {
                    "owner_kind": "reason",
                    "reason_run_id": row["reason_run_id"],
                    "status": row["status"],
                    "trigger": row["trigger"],
                }
            )
        plan_consumption_rows = conn.execute(
            select(
                plan_run_sources.c.fact_id,
                plan_runs.c.id.label("plan_run_id"),
                plan_runs.c.status,
                plan_runs.c.trigger,
            )
            .join(
                plan_runs,
                (plan_runs.c.task_id == plan_run_sources.c.task_id)
                & (plan_runs.c.id == plan_run_sources.c.plan_run_id),
            )
            .where(plan_run_sources.c.task_id == task["id"])
            .order_by(plan_runs.c.created_at, plan_runs.c.id)
        ).mappings()
        for row in plan_consumption_rows:
            fact_consumption.setdefault(row["fact_id"], []).append(
                {
                    "owner_kind": "plan",
                    "plan_run_id": row["plan_run_id"],
                    "status": row["status"],
                    "trigger": row["trigger"],
                }
            )

        def fact_provenance(fact_id: str) -> dict[str, Any]:
            source_intent_ids = source_intents_by_fact.get(fact_id, [])
            source_artifact_ids = list(
                dict.fromkeys(
                    artifact_id
                    for intent_id in source_intent_ids
                    for artifact_id in artifacts_by_intent.get(intent_id, [])
                )
            )
            source_intent_assessments = [
                {
                    "intent_id": intent_id,
                    "status": intent_rows_by_id[intent_id].get("assessment_status", "open")
                    or "open",
                    "reason": intent_rows_by_id[intent_id].get("assessment_reason", "") or "",
                    "assessed_at": intent_rows_by_id[intent_id].get("assessed_at"),
                }
                for intent_id in source_intent_ids
                if intent_id in intent_rows_by_id
                and intent_rows_by_id[intent_id].get("kind") == "explore"
            ]
            if not source_intent_assessments:
                assessment_status = "not_applicable"
            elif any(item["status"] == "open" for item in source_intent_assessments):
                assessment_status = "unassessed"
            else:
                assessment_status = "assessed"
            consumption = fact_consumption.get(fact_id, [])
            active_consumption = [
                item
                for item in consumption
                if item["status"]
                in {"pending", "claimed", "running", "concluding", "completed"}
            ]
            if not active_consumption:
                consumption_status = "unconsumed"
            elif any(item["status"] == "completed" for item in active_consumption):
                consumption_status = "consumed"
            else:
                consumption_status = "reserved"
            return {
                "source_intent_ids": source_intent_ids,
                "source_artifact_ids": source_artifact_ids,
                "source_intent_assessments": source_intent_assessments,
                "assessment_status": assessment_status,
                "consumption_status": consumption_status,
                "consumed_by_reason_runs": [
                    item for item in active_consumption if item["owner_kind"] == "reason"
                ],
                "consumed_by_plan_runs": [
                    item for item in active_consumption if item["owner_kind"] == "plan"
                ],
            }
        visible_goal_rows = [
            row
            for row in goal_rows
            if self._goal_evaluation_visible(
                conn,
                task["id"],
                row,
                include_archived=include_archived,
            )
        ]
        todo_change_runs = self._todo_change_runs(conn, task["id"])
        hint_relations = self._hint_relations(conn, task["id"])
        knowledge_relations = self._knowledge_relations(conn, task["id"])
        for relation in hint_relations.values():
            todo_ids = set(relation.get("associated_todo_ids", []))
            relation["associated_intent_ids"] = list(
                dict.fromkeys(
                    row["id"] for row in intent_rows if row.get("todo_id") in todo_ids
                )
            )
        for relation in knowledge_relations.values():
            todo_ids = set(relation.get("associated_todo_ids", []))
            relation["associated_intent_ids"] = list(
                dict.fromkeys(
                    row["id"] for row in intent_rows if row.get("todo_id") in todo_ids
                )
            )

        for row in fact_rows:
            fact_detail = dict(row)
            fact_detail.update(fact_provenance(row["id"]))
            self._add_detail(files, f"fact:{row['id']}", fact_detail)
        for row in intent_rows:
            intent = self.graph.get_intent(conn, task["id"], row["id"])
            assert intent is not None
            intent = intent.model_copy(
                update={
                    "from_facts": [
                        fact_id for fact_id in intent.from_facts if fact_id in visible_fact_ids
                    ],
                    "to_fact_id": (
                        intent.to_fact_id if intent.to_fact_id in visible_fact_ids else None
                    ),
                }
            )
            self._add_detail(files, f"intent:{row['id']}", intent.model_dump(mode="json"))
        for row in todo_rows:
            todo = self.todos.get(conn, task["id"], row["id"])
            assert todo is not None
            if not include_archived:
                todo = todo.model_copy(
                    update={
                        "sources": [
                            source
                            for source in todo.sources
                            if self.todos.source_exists(conn, task_id=task["id"], source=source)
                        ]
                    }
                )
            todo_detail = todo.model_dump(mode="json")
            change = todo_change_runs.get(row["id"], {})
            todo_detail.update(
                {
                    # Keep the historical Reason field for old consumers,
                    # while exposing independent Plan provenance explicitly.
                    "last_reason_run_id": (
                        change.get("run_id")
                        if change.get("run_kind") == "reason"
                        else None
                    ),
                    "last_plan_run_id": (
                        change.get("run_id")
                        if change.get("run_kind") == "plan"
                        else None
                    ),
                    "last_change_run_id": change.get("run_id"),
                    "last_change_run_kind": change.get("run_kind"),
                    "last_change_at": change.get("changed_at"),
                    "last_change_reason": todo.status_reason,
                }
            )
            self._add_detail(files, f"todo:{row['id']}", todo_detail)
        for row in artifact_rows:
            artifact = (
                self.artifacts.get_any(conn, task["id"], row["id"])
                if include_archived
                else self.artifacts.get(conn, task["id"], row["id"])
            )
            assert artifact is not None
            artifact = artifact.model_copy(
                update={
                    "intent_id": (
                        artifact.intent_id if artifact.intent_id in visible_intent_ids else None
                    )
                }
            )
            self._add_detail(
                files,
                f"artifact:{row['id']}",
                {
                    **artifact.model_dump(mode="json"),
                    "source_fact_ids": intent_sources_by_id.get(artifact.intent_id, []),
                    "associated_endpoint": _intent_marker(
                        intent_rows_by_id.get(artifact.intent_id, {}).get("description", ""),
                        "endpoint",
                    ),
                    "associated_targets": _json_list(
                        intent_rows_by_id.get(artifact.intent_id, {}).get("targets_json")
                    ),
                    "content_path": str(self.store.resolve(artifact.storage_key)),
                },
            )
        for row in hint_rows:
            hint_detail = dict(row)
            if include_archived:
                hint_detail.update(
                    hint_relations.get(
                        row["id"],
                        {
                            "processed": False,
                            "associated_todo_ids": [],
                            "associated_intent_ids": [],
                            "associated_reason_run_ids": [],
                            "associated_plan_run_ids": [],
                        },
                    )
                )
            self._add_detail(files, f"hint:{row['id']}", hint_detail)
        for row in vulnerability_rows:
            vulnerability = self.vulnerabilities.get(conn, task["id"], row["id"])
            assert vulnerability is not None
            vulnerability = vulnerability.model_copy(
                update={
                    "intent_id": (
                        vulnerability.intent_id
                        if vulnerability.intent_id in visible_intent_ids
                        else None
                    )
                }
            )
            self._add_detail(
                files,
                f"vulnerability:{row['id']}",
                vulnerability.model_dump(mode="json"),
            )
        for row in knowledge_rows:
            knowledge_detail = dict(row)
            knowledge_detail.update(knowledge_relations.get(row["id"], {}))
            self._add_detail(files, f"knowledge:{row['id']}", knowledge_detail)
        for row in visible_goal_rows:
            self._add_detail(files, f"goal_evaluation:{row['id']}", dict(row))

        graph = {
            "facts": [
                {
                    "id": row["id"],
                    "created_at": row["created_at"],
                    "archived": bool(row.get("archived", False)),
                    **fact_provenance(row["id"]),
                    "ref": f"fact:{row['id']}",
                    "detail": _detail_path(f"fact:{row['id']}"),
                }
                for row in fact_rows
            ],
            "intents": [
                {
                    "id": row["id"],
                    "todo_id": row["todo_id"],
                    "kind": row["kind"],
                    "priority": 50 if row.get("priority") is None else int(row["priority"]),
                    "status": row["status"],
                    "retry_count": int(row.get("retry_count") or 0),
                    "infrastructure_retry_count": int(
                        row.get("infrastructure_retry_count") or 0
                    ),
                    "last_failure_kind": row.get("last_failure_kind") or "",
                    "failure_class": row.get("failure_class") or "",
                    "assessment_status": row.get("assessment_status", "open") or "open",
                    "assessment_reason": row.get("assessment_reason", "") or "",
                    "assessed_at": row.get("assessed_at"),
                    "dedup_fingerprint": (
                        row.get("dedup_fingerprint")
                        or _json_object(row.get("plan_metadata_json")).get(
                            "dedup_fingerprint", ""
                        )
                        or _intent_fingerprint(
                            description=row["description"],
                            targets=_json_list(row.get("targets_json")),
                        )
                    ),
                    "ref": f"intent:{row['id']}",
                    "detail": _detail_path(f"intent:{row['id']}"),
                }
                for row in intent_rows
            ],
        }
        todo_index = [
            {
                "id": row["id"],
                "title": row["title"],
                "status": row["status"],
                "status_reason": row["status_reason"],
                "category": row["category"],
                "theme": row["theme"],
                "priority": row["priority"],
                "created_by": row["created_by"],
                "updated_at": row["updated_at"],
                "completed_at": row["completed_at"],
                "archived": bool(row.get("archived", False)),
                "last_reason_run_id": (
                    todo_change_runs.get(row["id"], {}).get("run_id")
                    if todo_change_runs.get(row["id"], {}).get("run_kind") == "reason"
                    else None
                ),
                "last_plan_run_id": (
                    todo_change_runs.get(row["id"], {}).get("run_id")
                    if todo_change_runs.get(row["id"], {}).get("run_kind") == "plan"
                    else None
                ),
                "last_change_run_id": todo_change_runs.get(row["id"], {}).get("run_id"),
                "last_change_run_kind": todo_change_runs.get(row["id"], {}).get("run_kind"),
                "last_change_at": todo_change_runs.get(row["id"], {}).get("changed_at"),
                "last_change_reason": row["status_reason"],
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
                "intent_id": (
                    row["intent_id"] if row["intent_id"] in visible_intent_ids else None
                ),
                "review_run_id": row.get("review_run_id"),
                "source_fact_ids": intent_sources_by_id.get(row["intent_id"], []),
                "associated_endpoint": _intent_marker(
                    intent_rows_by_id.get(row["intent_id"], {}).get("description", ""),
                    "endpoint",
                ),
                "associated_targets": _json_list(
                    intent_rows_by_id.get(row["intent_id"], {}).get("targets_json")
                ),
                "ref": f"artifact:{row['id']}",
                "detail": _detail_path(f"artifact:{row['id']}"),
            }
            for row in artifact_rows
        ]
        hint_index = [
            {
                "id": row["id"],
                "content": row["content"],
                "creator": row["creator"],
                **(
                    hint_relations.get(
                        row["id"],
                    {
                        "processed": False,
                        "associated_todo_ids": [],
                        "associated_intent_ids": [],
                        "associated_reason_run_ids": [],
                        "associated_plan_run_ids": [],
                    },
                    )
                    if include_archived
                    else {}
                ),
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
                "title": row["title"],
                "type": row["type"],
                "affected_target": row["affected_target"],
                "source": row["source"],
                "intent_id": (
                    row["intent_id"] if row["intent_id"] in visible_intent_ids else None
                ),
                "status_reason": row["status_reason"],
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
                "content": row["content"],
                "created_by": row["created_by"],
                **knowledge_relations.get(row["id"], {}),
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
            for row in visible_goal_rows
        ]
        goal_cycle_index = [dict(row) for row in goal_cycle_rows]
        files.update(
            {
                "graph.json": _json(graph),
                "todos.json": _json(todo_index),
                "artifacts.json": _json(artifact_index),
                "hints.json": _json(hint_index),
                "vulnerabilities.json": _json(vulnerability_index),
                "knowledge.json": _json(knowledge_index),
            }
        )
        if include_goal_history:
            files.update(
                {
                    "goal_evaluations.json": _json(goal_index),
                    "goal_cycles.json": _json(goal_cycle_index),
                }
            )
        return files

    @staticmethod
    def _add_detail(files: dict[str, str], ref: str, value: Any) -> None:
        files[_detail_path(ref)] = _json(value)

    @staticmethod
    def _todo_change_runs(conn, task_id: str) -> dict[str, dict[str, Any]]:
        changes: dict[str, dict[str, Any]] = {}
        rows = conn.execute(
            select(
                audit_events.c.reason_run_id,
                audit_events.c.plan_run_id,
                audit_events.c.payload_json,
                audit_events.c.created_at,
            )
            .where(
                audit_events.c.task_id == task_id,
                (
                    (
                        audit_events.c.reason_run_id.is_not(None)
                        & (audit_events.c.event_type == "reason.committed")
                    )
                    | (
                        audit_events.c.plan_run_id.is_not(None)
                        & (audit_events.c.event_type == "plan.workspace_committed")
                    )
                ),
            )
            .order_by(audit_events.c.created_at, audit_events.c.id)
        ).mappings()
        for row in rows:
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                payload = {}
            ids = [
                *payload.get("created_todo_ids", []),
                *payload.get("updated_todo_ids", []),
                *payload.get("deleted_todo_ids", []),
            ]
            for todo_id in ids:
                if isinstance(todo_id, str):
                    run_kind = "plan" if row.get("plan_run_id") else "reason"
                    changes[todo_id] = {
                        "run_id": row.get("plan_run_id") or row.get("reason_run_id"),
                        "run_kind": run_kind,
                        "changed_at": row.get("created_at"),
                    }
        return changes

    @staticmethod
    def _hint_relations(conn, task_id: str) -> dict[str, dict[str, Any]]:
        relations: dict[str, dict[str, Any]] = {}
        source_rows = conn.execute(
            select(todo_sources.c.source_id, todo_sources.c.todo_id)
            .where(
                todo_sources.c.task_id == task_id,
                todo_sources.c.source_type == "hint",
            )
        ).mappings()
        for row in source_rows:
            item = relations.setdefault(
                row["source_id"],
                {
                    "processed": False,
                    "associated_todo_ids": [],
                    "associated_intent_ids": [],
                    "associated_reason_run_ids": [],
                    "associated_plan_run_ids": [],
                },
            )
            item["associated_todo_ids"].append(row["todo_id"])
            # A Hint referenced by a durable Todo has already influenced the
            # graph, regardless of whether Reason or Plan created that Todo.
            item["processed"] = True
        rows = conn.execute(
            select(audit_events.c.reason_run_id, audit_events.c.payload_json)
            .where(
                audit_events.c.task_id == task_id,
                audit_events.c.reason_run_id.is_not(None),
                audit_events.c.event_type == "reason_run.created",
            )
        ).mappings()
        for row in rows:
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                payload = {}
            hint_id = payload.get("hint_id")
            if not isinstance(hint_id, str):
                continue
            item = relations.setdefault(
                hint_id,
                {
                    "processed": False,
                    "associated_todo_ids": [],
                    "associated_intent_ids": [],
                    "associated_reason_run_ids": [],
                    "associated_plan_run_ids": [],
                },
            )
            item["processed"] = True
            item["associated_reason_run_ids"].append(row["reason_run_id"])
        plan_rows = conn.execute(
            select(audit_events.c.plan_run_id, audit_events.c.payload_json)
            .where(
                audit_events.c.task_id == task_id,
                audit_events.c.plan_run_id.is_not(None),
                audit_events.c.event_type == "plan_run.created",
            )
        ).mappings()
        for row in plan_rows:
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                payload = {}
            hint_id = payload.get("hint_id")
            if not isinstance(hint_id, str):
                continue
            item = relations.setdefault(
                hint_id,
                {
                    "processed": False,
                    "associated_todo_ids": [],
                    "associated_intent_ids": [],
                    "associated_reason_run_ids": [],
                    "associated_plan_run_ids": [],
                },
            )
            item["processed"] = True
            item["associated_plan_run_ids"].append(row["plan_run_id"])
        for item in relations.values():
            item["associated_todo_ids"] = list(dict.fromkeys(item["associated_todo_ids"]))
            item["associated_intent_ids"] = list(dict.fromkeys(item["associated_intent_ids"]))
            item["associated_reason_run_ids"] = list(
                dict.fromkeys(item["associated_reason_run_ids"])
            )
            item["associated_plan_run_ids"] = list(
                dict.fromkeys(item["associated_plan_run_ids"])
            )
        return relations

    @staticmethod
    def _knowledge_relations(conn, task_id: str) -> dict[str, dict[str, Any]]:
        relations: dict[str, dict[str, Any]] = {}
        rows = conn.execute(
            select(todo_sources.c.source_id, todo_sources.c.todo_id)
            .where(
                todo_sources.c.task_id == task_id,
                todo_sources.c.source_type == "knowledge",
            )
        ).mappings()
        for row in rows:
            item = relations.setdefault(
                row["source_id"],
                {"associated_todo_ids": [], "associated_intent_ids": []},
            )
            item["associated_todo_ids"].append(row["todo_id"])
        for item in relations.values():
            item["associated_todo_ids"] = list(dict.fromkeys(item["associated_todo_ids"]))
            item["associated_intent_ids"] = list(dict.fromkeys(item["associated_intent_ids"]))
        return relations

    def _revision(self, row) -> ContextRevision:
        return ContextRevision(
            task_id=row["task_id"],
            revision=row["revision"],
            phase=row["phase"],
            intent_id=row["intent_id"],
            reason_run_id=row.get("reason_run_id"),
            plan_run_id=row.get("plan_run_id"),
            review_run_id=row.get("review_run_id"),
            lease_id=row["lease_id"],
            manifest_key=row["manifest_key"],
            manifest_path=str(self.store.resolve(row["manifest_key"])),
            content_hash=row["content_hash"],
            created_at=row["created_at"],
        )

    def _expand_one(
        self,
        conn,
        task_id: str,
        kind: str,
        item_id: str,
        *,
        include_archived: bool = False,
    ) -> Any:
        if kind == "fact":
            query = select(facts).where(facts.c.task_id == task_id, facts.c.id == item_id)
            if not include_archived:
                query = query.where(facts.c.archived.is_(False))
            row = conn.execute(query).mappings().one_or_none()
            if row is not None:
                return dict(row)
        elif kind == "intent":
            intent_query = select(intents.c.id).where(
                intents.c.task_id == task_id,
                intents.c.id == item_id,
            )
            if not include_archived:
                intent_query = intent_query.where(intents.c.archived.is_(False))
            visible = conn.execute(intent_query).scalar_one_or_none()
            intent = self.graph.get_intent(conn, task_id, item_id) if visible else None
            if intent is not None:
                facts_query = select(facts.c.id).where(
                    facts.c.task_id == task_id,
                    facts.c.id.in_(intent.from_facts),
                )
                if not include_archived:
                    facts_query = facts_query.where(facts.c.archived.is_(False))
                visible_from_facts = set(conn.execute(facts_query).scalars())
                visible_to_fact = None
                if intent.to_fact_id is not None:
                    to_fact_query = select(facts.c.id).where(
                        facts.c.task_id == task_id,
                        facts.c.id == intent.to_fact_id,
                    )
                    if not include_archived:
                        to_fact_query = to_fact_query.where(facts.c.archived.is_(False))
                    visible_to_fact = conn.execute(to_fact_query).scalar_one_or_none()
                return intent.model_copy(
                    update={
                        "from_facts": [
                            fact_id
                            for fact_id in intent.from_facts
                            if fact_id in visible_from_facts
                        ],
                        "to_fact_id": visible_to_fact,
                    }
                ).model_dump(mode="json")
        elif kind == "todo":
            todo = self.todos.get(conn, task_id, item_id)
            if todo is not None and (include_archived or not todo.archived):
                return todo.model_copy(
                    update={
                        "sources": [
                            source
                            for source in todo.sources
                            if self.todos.source_exists(conn, task_id=task_id, source=source)
                        ]
                    }
                ).model_dump(mode="json")
        elif kind == "artifact":
            artifact = (
                self.artifacts.get_any(conn, task_id, item_id)
                if include_archived
                else self.artifacts.get(conn, task_id, item_id)
            )
            if artifact is not None:
                visible_intent_id = self._visible_intent_id(
                    conn,
                    task_id,
                    artifact.intent_id,
                    include_archived=include_archived,
                )
                return {
                    **artifact.model_copy(
                        update={"intent_id": visible_intent_id}
                    ).model_dump(mode="json"),
                    "content": self.store.read_text(artifact.storage_key),
                }
        elif kind == "hint":
            hint_query = select(task_hints).where(
                task_hints.c.task_id == task_id,
                task_hints.c.id == item_id,
            )
            if not include_archived:
                hint_query = hint_query.where(task_hints.c.archived.is_(False))
            row = conn.execute(hint_query).mappings().one_or_none()
            if row is not None:
                return dict(row)
        elif kind == "vulnerability":
            vulnerability_query = select(vulnerabilities).where(
                vulnerabilities.c.task_id == task_id,
                vulnerabilities.c.id == item_id,
            )
            if not include_archived:
                vulnerability_query = vulnerability_query.where(vulnerabilities.c.archived.is_(False))
            vulnerability = conn.execute(vulnerability_query).mappings().one_or_none()
            if vulnerability is not None:
                model = self.vulnerabilities.get(conn, task_id, item_id)
                assert model is not None
                return model.model_copy(
                    update={
                        "intent_id": self._visible_intent_id(
                            conn,
                            task_id,
                            model.intent_id,
                            include_archived=include_archived,
                        )
                    }
                ).model_dump(mode="json")
        elif kind == "knowledge":
            task = self.repository.task(conn, task_id)
            assert task is not None
            knowledge_query = select(project_knowledge).where(
                project_knowledge.c.project_id == task["project_id"],
                project_knowledge.c.id == item_id,
                project_knowledge.c.enabled.is_(True),
            )
            if not include_archived:
                knowledge_query = knowledge_query.where(project_knowledge.c.archived.is_(False))
            row = conn.execute(knowledge_query).mappings().one_or_none()
            if row is not None:
                return dict(row)
        elif kind == "goal_evaluation":
            row = conn.execute(
                select(goal_evaluations).where(
                    goal_evaluations.c.task_id == task_id,
                    goal_evaluations.c.id == item_id,
                )
            ).mappings().one_or_none()
            if row is not None and self._goal_evaluation_visible(
                conn,
                task_id,
                row,
                include_archived=include_archived,
            ):
                return dict(row)
        raise NotFoundError(f"Context entry not found: {kind}:{item_id}")

    @staticmethod
    def _visible_intent_id(
        conn,
        task_id: str,
        intent_id: str | None,
        *,
        include_archived: bool = False,
    ) -> str | None:
        if intent_id is None:
            return None
        query = select(intents.c.id).where(
            intents.c.task_id == task_id,
            intents.c.id == intent_id,
        )
        if not include_archived:
            query = query.where(intents.c.archived.is_(False))
        return conn.execute(query).scalar_one_or_none()

    @staticmethod
    def _goal_evaluation_visible(
        conn,
        task_id: str,
        row,
        *,
        include_archived: bool = False,
    ) -> bool:
        table_map = {
            "fact": facts,
            "artifact": artifacts,
            "vulnerability": vulnerabilities,
            "todo": todos,
            "hint": task_hints,
        }
        for ref in json.loads(row["evidence_refs_json"] or "[]"):
            kind, separator, item_id = ref.partition(":")
            table = table_map.get(kind)
            if not separator or table is None:
                continue
            query = select(table.c.id).where(
                table.c.task_id == task_id,
                table.c.id == item_id,
            )
            if not include_archived:
                query = query.where(table.c.archived.is_(False))
            visible = conn.execute(query).scalar_one_or_none()
            if visible is None:
                return False
        return True


def _preview(value: str, limit: int = PREVIEW_CHARS) -> str:
    normalized = " ".join(value.split())
    return normalized if len(normalized) <= limit else normalized[:limit] + "…"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def _json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return [item for item in parsed if isinstance(item, str)] if isinstance(parsed, list) else []


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _intent_marker(description: str, marker: str) -> str:
    for part in re.split(r"[;；\n]", description or ""):
        key, separator, value = part.partition(":")
        if separator and key.strip().casefold() == marker.casefold():
            return " ".join(value.split())
    return ""


def _intent_fingerprint(*, description: str, targets: list[str]) -> str:
    """Stable Plan dedup signature: target + endpoint + test vector.

    Plan workers are asked to annotate these values in the human-readable
    Intent description.  Older Intents do not have structured metadata, so
    the normalized description remains the backwards-compatible fallback for
    the missing endpoint/vector components.
    """

    markers = {
        marker: _intent_marker(description, marker).casefold()
        for marker in ("endpoint", "test_vector", "test vector", "target")
    }
    normalized_description = " ".join((description or "").split()).casefold()
    payload = {
        "target": sorted(set(targets or [])),
        "endpoint": markers.get("endpoint") or normalized_description,
        "test_vector": markers.get("test_vector") or normalized_description,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


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
