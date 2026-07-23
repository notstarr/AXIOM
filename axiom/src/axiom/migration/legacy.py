from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from alembic import command
from sqlalchemy import Engine, delete, select

from axiom.cli import alembic_config
from axiom.modules.artifacts.tables import artifacts
from axiom.modules.audit.tables import audit_events
from axiom.modules.context.tables import context_revisions
from axiom.modules.credentials.tables import credentials
from axiom.modules.external_reports.tables import external_report_attempts
from axiom.modules.goals.tables import goal_evaluations
from axiom.modules.graph.tables import facts, intent_sources, intents
from axiom.modules.knowledge.tables import project_knowledge
from axiom.modules.mcp.tables import mcp_servers, mcp_tools
from axiom.modules.projects.tables import projects
from axiom.modules.reason.tables import reason_workspaces
from axiom.modules.settings.schemas import RuntimeConfig
from axiom.modules.settings.tables import runtime_settings
from axiom.modules.tasks.tables import task_hints, tasks
from axiom.modules.todos.tables import todo_sources, todos
from axiom.modules.vulnerabilities.tables import vulnerabilities
from axiom.modules.workers.pi_config import canonicalize_pi_worker_config
from axiom.modules.workers.tables import workers
from axiom.shared.clock import utcnow
from axiom.shared.database import create_sqlite_engine
from axiom.shared.ids import id_counters
from axiom.shared import model_registry as _model_registry  # noqa: F401


_VALID_CATEGORIES = {
    "recon",
    "authentication",
    "authorization",
    "injection",
    "business_logic",
    "file",
    "network",
    "configuration",
    "client_side",
    "other",
}


@dataclass(slots=True)
class MigrationReport:
    source_db: str
    target_db: str
    source_data_dir: str
    target_data_dir: str
    counts: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    verification: dict[str, Any] = field(default_factory=dict)

    def add(self, kind: str, amount: int = 1) -> None:
        self.counts[kind] = self.counts.get(kind, 0) + amount

    def skip(self, kind: str, amount: int = 1) -> None:
        self.skipped[kind] = self.skipped.get(kind, 0) + amount

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_db": self.source_db,
            "target_db": self.target_db,
            "source_data_dir": self.source_data_dir,
            "target_data_dir": self.target_data_dir,
            "counts": dict(sorted(self.counts.items())),
            "skipped": dict(sorted(self.skipped.items())),
            "warnings": self.warnings,
            "verification": self.verification,
        }


def migrate_legacy_database(
    source_db: str | Path,
    target_db: str | Path,
    *,
    source_data_dir: str | Path,
    target_data_dir: str | Path,
) -> MigrationReport:
    """Migrate a frozen Cairn database into the current schema.

    The source is opened read-only.  The target must not already contain a
    database; the function upgrades it to the current Alembic head before
    inserting data.  Legacy tasks are deliberately stopped and marked
    ``legacy_readonly``: the migration preserves history, not an executable
    lease or a second live dispatcher.
    """

    source_path = Path(source_db).expanduser().resolve()
    target_path = Path(target_db).expanduser().resolve()
    source_root = Path(source_data_dir).expanduser().resolve()
    target_root = Path(target_data_dir).expanduser().resolve()
    if not source_path.is_file():
        raise ValueError(f"Legacy database not found: {source_path}")
    if source_path == target_path:
        raise ValueError("Source and target database must be different")
    if target_path.exists() and target_path.stat().st_size:
        raise ValueError(f"Target database already exists: {target_path}")

    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_root.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(target_path), "head")
    engine = create_sqlite_engine(target_path)
    report = MigrationReport(
        source_db=str(source_path),
        target_db=str(target_path),
        source_data_dir=str(source_root),
        target_data_dir=str(target_root),
    )
    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    try:
        _migrate(source, engine, source_root, target_root, report)
    except BaseException:
        engine.dispose()
        raise
    finally:
        source.close()
        engine.dispose()
    return report


def _migrate(
    source: sqlite3.Connection,
    engine: Engine,
    source_root: Path,
    target_root: Path,
    report: MigrationReport,
) -> None:
    now = utcnow()
    source_projects = _rows(source, "projects")
    source_tasks = _rows(source, "tasks")
    task_ids = {str(row["id"]) for row in source_tasks if row.get("id")}
    project_ids = {str(row["id"]) for row in source_projects if row.get("id")}
    orphan_projects = {
        str(row.get("project_id"))
        for row in source_tasks
        if row.get("project_id") and str(row["project_id"]) not in project_ids
    }
    if orphan_projects:
        report.warn(f"Created archived placeholder projects for {len(orphan_projects)} orphan references")

    with engine.begin() as conn:
        _insert_projects(conn, source_projects, orphan_projects, report, now)
        _insert_tasks(conn, source_tasks, orphan_projects, report, now)
        _insert_hints(conn, source, task_ids, report, now)
        _insert_facts(conn, source, task_ids, report, now)
        migrated_intents = _insert_intents(conn, source, task_ids, report, now)
        _insert_intent_sources(conn, source, migrated_intents, report)
        _insert_todos(conn, source, task_ids, report)
        _insert_todo_sources(conn, source, task_ids, report)
        _insert_workers(conn, source, report, now)
        _insert_artifacts(conn, source, source_root, target_root, migrated_intents, report, now)
        _insert_context_revisions(conn, source, source_root, target_root, task_ids, report, now)
        _insert_reason_workspaces(conn, source, task_ids, report, now)
        _insert_knowledge(conn, source, project_ids, report, now)
        _insert_credentials(conn, source, project_ids, report, now)
        _insert_vulnerabilities(conn, source, task_ids, migrated_intents, report, now)
        _insert_mcp(conn, source, report, now)
        _insert_external_reports(conn, source, task_ids, report, now)
        _insert_runtime_settings(conn, source, report, now)
        _insert_audit(conn, source, report)
        _insert_goal_evaluations(conn, source, task_ids, report, now)
        _rebuild_counters(conn, report)

    _verify(engine, report)


def _insert_projects(conn, rows, orphan_projects, report, now) -> None:
    for row in rows:
        project_id = str(row["id"])
        conn.execute(
            projects.insert().values(
                id=project_id,
                title=str(row.get("title") or project_id),
                description=str(row.get("description") or ""),
                status="archived" if row.get("status") == "archived" else "active",
                created_at=row.get("created_at") or now,
                updated_at=row.get("updated_at") or row.get("created_at") or now,
            )
        )
        report.add("projects")
    if orphan_projects:
        created_at = now
        for project_id in sorted(orphan_projects):
            conn.execute(
                projects.insert().values(
                    id=project_id,
                    title="迁移恢复的遗留项目",
                    description="Source database contained a Task without its Project row.",
                    status="archived",
                    created_at=created_at,
                    updated_at=created_at,
                )
            )
            report.add("projects_recovered")


def _insert_tasks(conn, rows, orphan_projects, report, now) -> None:
    for row in rows:
        task_id = str(row["id"])
        raw_status = str(row.get("status") or "stopped")
        status = raw_status if raw_status in {"completed", "failed"} else "stopped"
        if raw_status in {"running", "pending"}:
            report.warn(f"Task {task_id} was {raw_status}; imported as stopped/read-only")
        scope = _scope(row.get("scope_json"), bool(row.get("scope_enabled", 0)))
        worker_pool = _json_list(row.get("worker_pool_json"))
        conn.execute(
            tasks.insert().values(
                id=task_id,
                project_id=str(row.get("project_id") or next(iter(orphan_projects), "legacy-orphans")),
                title=str(row.get("title") or task_id),
                origin_host=str(row.get("origin_host") or "legacy.invalid"),
                origin_scheme=row.get("origin_scheme"),
                origin_port=str(row["origin_port"]) if row.get("origin_port") is not None else None,
                origin_input=str(row.get("origin_input") or row.get("origin_host") or "legacy.invalid"),
                goal=str(row.get("goal") or "遗留任务，原始目标未记录"),
                status=status,
                phase="none",
                scope_json=json.dumps(scope, ensure_ascii=False),
                worker_pool_json=json.dumps(worker_pool, ensure_ascii=False),
                guard_enabled=bool(row.get("guard_enabled", 0)),
                external_report_enabled=bool(row.get("external_report_enabled", 0)),
                legacy_readonly=True,
                created_at=row.get("created_at") or now,
                updated_at=row.get("updated_at") or row.get("created_at") or now,
                completed_at=(row.get("completed_at") or row.get("updated_at") or now)
                if status == "completed"
                else None,
            )
        )
        report.add("tasks")


def _insert_hints(conn, source, task_ids, report, now) -> None:
    rows = [*_rows(source, "task_hints"), *_rows(source, "hints")]
    for row in rows:
        task_id = str(row.get("task_id") or row.get("project_id") or "")
        if task_id not in task_ids:
            report.skip("hints")
            continue
        conn.execute(
            task_hints.insert().values(
                id=str(row.get("id") or f"legacy-h{report.counts.get('hints', 0) + 1}"),
                task_id=task_id,
                content=str(row.get("content") or ""),
                creator=str(row.get("creator") or "legacy"),
                created_at=row.get("created_at") or now,
                archived=bool(row.get("archived", 0)),
            )
        )
        report.add("hints")


def _insert_facts(conn, source, task_ids, report, now) -> None:
    for row in _rows(source, "facts"):
        task_id = str(row.get("task_id") or row.get("project_id") or "")
        if task_id not in task_ids:
            report.skip("facts")
            continue
        conn.execute(
            facts.insert().values(
                id=str(row.get("id") or f"legacy-f{report.counts.get('facts', 0) + 1}"),
                task_id=task_id,
                description=str(row.get("description") or ""),
                archived=bool(row.get("archived", 0)),
                created_at=row.get("created_at") or now,
            )
        )
        report.add("facts")


def _insert_intents(conn, source, task_ids, report, now) -> set[tuple[str, str]]:
    migrated: set[tuple[str, str]] = set()
    facts_by_task: dict[str, set[str]] = {}
    for fact in _rows(source, "facts"):
        task_id = str(fact.get("task_id") or fact.get("project_id") or "")
        facts_by_task.setdefault(task_id, set()).add(str(fact.get("id") or ""))
    for row in _rows(source, "intents"):
        task_id = str(row.get("task_id") or row.get("project_id") or "")
        intent_id = str(row.get("id") or "")
        if task_id not in task_ids or not intent_id:
            report.skip("intents")
            continue
        kind = _intent_kind(row, intent_id)
        status = str(row.get("status") or "cancelled")
        if status not in {"pending", "claimed", "running", "concluding", "completed", "failed", "cancelled"}:
            status = "cancelled"
        if status in {"pending", "claimed", "running", "concluding"}:
            status = "cancelled"
        source_facts = facts_by_task.get(task_id, set())
        to_fact_id = str(row["to_fact_id"]) if row.get("to_fact_id") in source_facts else None
        if row.get("to_fact_id") and to_fact_id is None:
            report.warn(f"Cleared missing to_fact_id on {task_id}/{intent_id}")
        conn.execute(
            intents.insert().values(
                id=intent_id,
                task_id=task_id,
                todo_id=str(row["todo_id"]) if row.get("todo_id") else None,
                kind=kind,
                description=str(row.get("description") or kind),
                status=status,
                creator=str(row.get("creator") or "legacy"),
                worker=row.get("worker"),
                worker_pool_json=json.dumps(_json_list(row.get("worker_pool_json")), ensure_ascii=False),
                targets_json=json.dumps(_json_list(row.get("targets_json")), ensure_ascii=False),
                execute_timeout_seconds=row.get("execute_timeout_seconds"),
                conclude_timeout_seconds=row.get("conclude_timeout_seconds"),
                lease_id=None,
                claimed_at=None,
                last_heartbeat_at=row.get("last_heartbeat_at"),
                retry_count=int(row.get("retry_count") or 0),
                infrastructure_retry_count=int(row.get("infrastructure_retry_count") or 0),
                next_retry_at=None,
                last_failure_kind=str(row.get("last_failure_kind") or ""),
                failure_class=str(row.get("failure_class") or ""),
                activity_kind=str(row.get("activity_kind") or ""),
                activity_summary=str(row.get("activity_summary") or ""),
                activity_tool=str(row.get("activity_tool") or ""),
                activity_started_at=row.get("activity_started_at"),
                activity_updated_at=row.get("activity_updated_at"),
                to_fact_id=to_fact_id,
                created_at=row.get("created_at") or now,
                concluded_at=row.get("concluded_at"),
                archived=bool(row.get("archived", 0)),
            )
        )
        migrated.add((task_id, intent_id))
        report.add("intents")
    return migrated


def _insert_intent_sources(conn, source, migrated, report) -> None:
    fact_keys = {
        (str(row.get("task_id") or row.get("project_id") or ""), str(row.get("id")))
        for row in _rows(source, "facts")
    }
    for row in _rows(source, "intent_sources"):
        key = (str(row.get("task_id") or row.get("project_id") or ""), str(row.get("intent_id") or ""))
        fact_key = (key[0], str(row.get("fact_id") or ""))
        if key not in migrated or fact_key not in fact_keys:
            report.skip("intent_sources")
            continue
        conn.execute(intent_sources.insert().values(task_id=key[0], intent_id=key[1], fact_id=fact_key[1]))
        report.add("intent_sources")


def _insert_todos(conn, source, task_ids, report) -> None:
    for row in _rows(source, "todos"):
        task_id = str(row.get("task_id") or "")
        if task_id not in task_ids:
            report.skip("todos")
            continue
        category = str(row.get("category") or "other")
        if category not in _VALID_CATEGORIES:
            category = "other"
        status = str(row.get("status") or "pending")
        if status not in {"pending", "active", "completed", "rejected", "obsolete"}:
            status = "obsolete"
        conn.execute(
            todos.insert().values(
                id=str(row.get("id")),
                task_id=task_id,
                title=str(row.get("title") or "Legacy Todo"),
                category=category,
                theme=str(row.get("theme") or ""),
                priority=max(0, min(100, int(row.get("priority") or 50))),
                status=status,
                created_by=str(row.get("created_by") or "legacy"),
                status_reason=str(row.get("status_reason") or ""),
                created_at=row.get("created_at") or "1970-01-01T00:00:00Z",
                updated_at=row.get("updated_at") or row.get("created_at") or "1970-01-01T00:00:00Z",
                completed_at=row.get("completed_at"),
                archived=bool(row.get("archived", 0)),
            )
        )
        report.add("todos")


def _insert_todo_sources(conn, source, task_ids, report) -> None:
    for row in _rows(source, "todo_sources"):
        task_id = str(row.get("task_id") or "")
        if task_id not in task_ids:
            report.skip("todo_sources")
            continue
        source_type = str(row.get("source_type") or "fact")
        if source_type not in {"fact", "hint", "artifact", "vulnerability", "knowledge", "origin"}:
            report.skip("todo_sources")
            continue
        conn.execute(
            todo_sources.insert().values(
                task_id=task_id,
                todo_id=str(row.get("todo_id")),
                source_type=source_type,
                source_id=str(row.get("source_id")),
            )
        )
        report.add("todo_sources")


def _insert_workers(conn, source, report, now) -> None:
    cooldowns = {
        str(row["worker_name"]): row.get("cooldown_until")
        for row in _rows(source, "worker_runtime_states")
    }
    for row in [*_rows(source, "dispatch_workers"), *_rows(source, "workers")]:
        config = _json_obj(row.get("config_json"))
        try:
            config = canonicalize_pi_worker_config(config)
        except Exception as exc:
            report.warn(f"Worker {row.get('name')} config normalized with defaults: {exc}")
            config = {}
        conn.execute(
            workers.insert().values(
                name=str(row.get("name")),
                worker_type=str(row.get("worker_type") or "pi"),
                capabilities_json=json.dumps(_json_list(row.get("capabilities_json")), ensure_ascii=False),
                max_running=max(1, int(row.get("max_running") or 1)),
                priority=int(row.get("priority") or 0),
                config_json=json.dumps(config, ensure_ascii=False),
                disabled=bool(row.get("disabled", 0)),
                cooldown_until=cooldowns.get(str(row.get("name"))),
                created_at=row.get("created_at") or row.get("updated_at") or now,
                updated_at=row.get("updated_at") or now,
            )
        )
        report.add("workers")


def _insert_artifacts(conn, source, source_root, target_root, migrated_intents, report, now) -> None:
    used: set[tuple[str, str]] = set()
    for row in _rows(source, "artifacts"):
        task_id = str(row.get("task_id") or "")
        if not task_id:
            report.skip("artifacts")
            continue
        storage_key, content_hash, size = _materialize_file(
            row.get("path"), source_root, target_root, report
        )
        if (task_id, storage_key) in used:
            storage_key = f"legacy-import/{task_id}/{row.get('id')}/{Path(storage_key).name}"
        used.add((task_id, storage_key))
        intent_id = str(row["intent_id"]) if row.get("intent_id") else None
        if intent_id and (task_id, intent_id) not in migrated_intents:
            report.warn(f"Cleared missing artifact intent {task_id}/{intent_id}")
            intent_id = None
        conn.execute(
            artifacts.insert().values(
                id=str(row.get("id")),
                task_id=task_id,
                intent_id=intent_id,
                artifact_type=str(row.get("artifact_type") or "legacy"),
                storage_key=storage_key,
                summary=str(row.get("summary") or ""),
                content_hash=content_hash or str(row.get("content_hash") or ""),
                size=size if size is not None else int(row.get("size") or 0),
                created_by=str(row.get("created_by") or "legacy"),
                created_at=row.get("created_at") or now,
                archived=bool(row.get("archived", 0)),
            )
        )
        report.add("artifacts")


def _insert_context_revisions(conn, source, source_root, target_root, task_ids, report, now) -> None:
    for row in _rows(source, "context_revisions"):
        task_id = str(row.get("task_id") or row.get("project_id") or "")
        if task_id not in task_ids:
            report.skip("context_revisions")
            continue
        revision = int(row.get("revision") or 1)
        base_key = f"contexts/{task_id}/legacy/r{revision:06d}"
        indexes = {}
        for old_key, name in (
            ("graph_path", "graph"),
            ("todo_path", "todos"),
            ("artifact_index_path", "artifacts"),
            ("hints_path", "hints"),
            ("knowledge_path", "knowledge"),
        ):
            key, _, _ = _materialize_file(row.get(old_key), source_root, target_root, report)
            indexes[name] = key
        manifest = {
            "task_id": task_id,
            "revision": revision,
            "phase": "legacy",
            "intent_id": None,
            "lease_id": None,
            "legacy": True,
            "indexes": indexes,
            "source_hash_manifest": _json_obj(row.get("hash_manifest_json")),
            "detail_resolution": "Legacy context index retained for read-only history.",
        }
        manifest_key = f"{base_key}/manifest.json"
        _write_json(target_root / manifest_key, manifest)
        content_hash = _sha256(target_root / manifest_key)
        conn.execute(
            context_revisions.insert().values(
                task_id=task_id,
                revision=revision,
                phase="legacy",
                intent_id=None,
                lease_id=None,
                manifest_key=manifest_key,
                content_hash=content_hash,
                created_at=row.get("created_at") or now,
            )
        )
        report.add("context_revisions")


def _insert_reason_workspaces(conn, source, task_ids, report, now) -> None:
    for row in _rows(source, "reason_workspaces"):
        task_id = str(row.get("task_id") or row.get("project_id") or "")
        workspace_id = str(row.get("id") or "")
        if task_id not in task_ids or not workspace_id:
            report.skip("reason_workspaces")
            continue
        intent_id = f"legacy_reason_{workspace_id}"
        status = str(row.get("status") or "abandoned")
        status = status if status in {"committed", "abandoned"} else "abandoned"
        concluded_at = row.get("committed_at") or row.get("updated_at") or now
        conn.execute(
            intents.insert().values(
                id=intent_id,
                task_id=task_id,
                todo_id=None,
                kind="reason",
                description=f"Legacy Reason workspace {workspace_id}",
                status="completed" if status == "committed" else "cancelled",
                creator="migration.legacy",
                worker=row.get("worker"),
                worker_pool_json="[]",
                targets_json="[]",
                execute_timeout_seconds=None,
                conclude_timeout_seconds=None,
                lease_id=None,
                claimed_at=None,
                last_heartbeat_at=None,
                retry_count=0,
                infrastructure_retry_count=0,
                next_retry_at=None,
                last_failure_kind="",
                failure_class="",
                activity_kind="",
                activity_summary="",
                activity_tool="",
                activity_started_at=None,
                activity_updated_at=None,
                to_fact_id=None,
                created_at=row.get("created_at") or now,
                concluded_at=concluded_at,
                archived=False,
            )
        )
        draft_json, original_draft = _safe_legacy_draft(row.get("draft_json"))
        result_json = _json_obj(row.get("result_json"))
        if original_draft is not None:
            result_json["legacy_original_draft"] = original_draft
        conn.execute(
            reason_workspaces.insert().values(
                id=workspace_id,
                task_id=task_id,
                intent_id=intent_id,
                lease_id=f"legacy-{workspace_id}",
                status=status,
                context_revision=int(row.get("context_revision") or 1),
                draft_json=draft_json,
                worker=str(row.get("worker") or "legacy"),
                failure_reason=str(row.get("failure_reason") or ""),
                result_json=json.dumps(result_json, ensure_ascii=False),
                created_at=row.get("created_at") or now,
                updated_at=row.get("updated_at") or row.get("created_at") or now,
                committed_at=row.get("committed_at"),
            )
        )
        report.add("reason_workspaces")
        report.add("legacy_reason_intents")


def _insert_knowledge(conn, source, project_ids, report, now) -> None:
    used: set[tuple[str, str]] = set()
    for row in [*_rows(source, "project_knowledge"), *_rows(source, "blackboard_items")]:
        project_id = str(row.get("project_id") or row.get("group_id") or "")
        if project_id not in project_ids:
            report.skip("knowledge")
            continue
        hint = str(row.get("hint") or "").strip()
        content = str(row.get("content") or "")
        if hint:
            content = f"适用提示：{hint}\n\n{content}"
        category = "hint" if hint else "note"
        knowledge_id = str(row.get("id") or f"legacy-k{report.counts.get('knowledge', 0) + 1}")
        if (project_id, knowledge_id) in used:
            knowledge_id = f"legacy-{knowledge_id}-{report.counts.get('knowledge', 0) + 1}"
        used.add((project_id, knowledge_id))
        conn.execute(
            project_knowledge.insert().values(
                id=knowledge_id,
                project_id=project_id,
                title=str(row.get("title") or "Legacy knowledge"),
                content=content,
                category=category,
                created_by=str(row.get("created_by") or row.get("creator") or "legacy"),
                created_at=row.get("created_at") or now,
                updated_at=row.get("updated_at") or row.get("created_at") or now,
                enabled=True,
                archived=bool(row.get("archived", 0)),
            )
        )
        report.add("knowledge")


def _insert_credentials(conn, source, project_ids, report, now) -> None:
    for row in _rows(source, "credentials"):
        project_id = str(row.get("project_id") or row.get("group_id") or "")
        if project_id not in project_ids:
            report.skip("credentials")
            continue
        domains = _json_list(row.get("domains_json"))
        auth_type = _credential_type(row.get("auth_type"))
        data = _json_obj(row.get("credentials_json"))
        if row.get("notes"):
            data.setdefault("_legacy_notes", str(row["notes"]))
        data.setdefault("_legacy_domains", domains)
        conn.execute(
            credentials.insert().values(
                id=str(row.get("id") or f"legacy-cred-{report.counts.get('credentials', 0) + 1}"),
                project_id=project_id,
                name=str(row.get("label") or "Legacy credential"),
                credential_type=auth_type,
                target_host=str(domains[0]) if domains else "",
                data_json=json.dumps(data, ensure_ascii=False),
                enabled=bool(row.get("active", row.get("enabled", 1))),
                created_at=row.get("created_at") or row.get("captured_at") or now,
                updated_at=row.get("updated_at") or row.get("captured_at") or now,
            )
        )
        report.add("credentials")


def _insert_vulnerabilities(conn, source, task_ids, migrated_intents, report, now) -> None:
    rows = [*_rows(source, "vulnerabilities"), *_rows(source, "vulns")]
    used: set[tuple[str, str]] = set()
    for row in rows:
        task_id = str(row.get("task_id") or row.get("project_id") or "")
        if task_id not in task_ids:
            report.skip("vulnerabilities")
            continue
        intent_id = str(row["intent_id"]) if row.get("intent_id") else None
        if intent_id and (task_id, intent_id) not in migrated_intents:
            intent_id = None
        vulnerability_id = str(row.get("id"))
        if (task_id, vulnerability_id) in used:
            vulnerability_id = f"legacy-{vulnerability_id}-{report.counts.get('vulnerabilities', 0) + 1}"
        used.add((task_id, vulnerability_id))
        conn.execute(
            vulnerabilities.insert().values(
                id=vulnerability_id,
                task_id=task_id,
                intent_id=intent_id,
                title=str(row.get("title") or "Legacy vulnerability"),
                type=str(row.get("type") or "other"),
                severity=str(row.get("severity") or "medium"),
                description=str(row.get("description") or ""),
                prerequisites=str(row.get("prerequisites") or ""),
                impact=str(row.get("impact") or ""),
                reproduction_steps_json=_json_text(row.get("reproduction_steps_json"), "[]"),
                remediation=str(row.get("remediation") or row.get("recommendation") or ""),
                curl_command=str(row.get("curl_command") or ""),
                evidence_json=_json_text(row.get("evidence_json"), "[]"),
                affected_target=str(row.get("affected_target") or row.get("affected_scope") or ""),
                status=str(row.get("status") or "待验证"),
                status_reason=str(row.get("status_reason") or ""),
                source=str(row.get("source") or "legacy"),
                reported_by=str(row.get("reported_by") or "legacy"),
                created_at=row.get("created_at") or now,
                updated_at=row.get("updated_at") or row.get("created_at") or now,
                archived=bool(row.get("archived", 0)),
            )
        )
        report.add("vulnerabilities")


def _insert_mcp(conn, source, report, now) -> None:
    server_ids = set()
    for row in _rows(source, "mcp_servers"):
        server_id = str(row.get("id"))
        server_ids.add(server_id)
        conn.execute(
            mcp_servers.insert().values(
                id=server_id,
                name=str(row.get("name") or server_id),
                transport=str(row.get("transport") or "stdio"),
                command=str(row.get("command") or ""),
                args_json=_json_text(row.get("args_json"), "[]"),
                env_json=_json_text(row.get("env_json"), "{}"),
                url=str(row.get("url") or ""),
                source_format=str(row.get("source_format") or "json"),
                raw_config_json=_json_text(row.get("raw_config_json") or row.get("raw_config"), "{}"),
                disabled=bool(row.get("disabled", 0)),
                discovery_status=str(row.get("discovery_status") or "pending"),
                discovery_error=str(row.get("discovery_error") or ""),
                discovered_at=str(row.get("discovered_at") or ""),
                server_info_json=_json_text(row.get("server_info_json"), "{}"),
                instructions=str(row.get("instructions") or ""),
                created_at=row.get("created_at") or now,
                updated_at=row.get("updated_at") or now,
            )
        )
        report.add("mcp_servers")
    for row in [*_rows(source, "mcp_tools"), *_rows(source, "mcp_server_tools")]:
        server_id = str(row.get("server_id") or "")
        if server_id not in server_ids:
            report.skip("mcp_tools")
            continue
        conn.execute(
            mcp_tools.insert().values(
                id=str(row.get("id")),
                server_id=server_id,
                name=str(row.get("name") or "legacy-tool"),
                title=str(row.get("title") or ""),
                description=str(row.get("description") or ""),
                input_schema_json=_json_text(row.get("input_schema_json"), "{}"),
                output_schema_json=_json_text(row.get("output_schema_json"), "{}"),
                annotations_json=_json_text(row.get("annotations_json"), "{}"),
                enabled=bool(row.get("enabled", 1)),
                source=str(row.get("source") or "legacy"),
                created_at=row.get("created_at") or now,
                updated_at=row.get("updated_at") or now,
            )
        )
        report.add("mcp_tools")


def _insert_external_reports(conn, source, task_ids, report, now) -> None:
    for row in _rows(source, "external_report_attempts"):
        task_id = str(row.get("task_id") or "")
        if task_id not in task_ids:
            report.skip("external_reports")
            continue
        response_json = row.get("response_json")
        response_body = _json_text(response_json, "") if response_json not in (None, "") else ""
        status = str(row.get("status") or "failed")
        if status not in {"running", "succeeded", "failed"}:
            status = "failed"
        conn.execute(
            external_report_attempts.insert().values(
                id=str(row.get("id")),
                task_id=task_id,
                vulnerability_id=str(row.get("vulnerability_id") or ""),
                attempt_no=max(1, int(row.get("attempt_count") or row.get("attempt_no") or 1)),
                status=status,
                endpoint=str(row.get("platform") or row.get("resolved_project") or "legacy"),
                payload_json=_json_text(row.get("payload_json"), "{}"),
                response_status=None,
                response_body=response_body,
                error=str(row.get("last_error") or row.get("error") or ""),
                created_at=row.get("created_at") or now,
                updated_at=row.get("updated_at") or now,
                completed_at=row.get("updated_at") if status != "running" else None,
            )
        )
        report.add("external_reports")


def _insert_runtime_settings(conn, source, report, now) -> None:
    rows = _rows(source, "runtime_settings")
    source_row = rows[0] if rows else {}
    old = _json_obj(source_row.get("config_json"))
    field_map = {
        "interval_seconds": "runner_interval_seconds",
        "max_workers": "max_parallel",
        "heartbeat_interval_seconds": "heartbeat_interval_seconds",
        "lease_timeout_seconds": "lease_timeout_seconds",
        "retry_base_seconds": "retry_base_seconds",
        "worker_failure_cooldown_seconds": "worker_failure_cooldown_seconds",
        "bootstrap_execute_seconds": "bootstrap_execute_seconds",
        "bootstrap_conclude_seconds": "bootstrap_conclude_seconds",
        "reason_execute_seconds": "reason_execute_seconds",
        "reason_batch_size": "reason_batch_size",
        "reason_low_watermark": "reason_low_watermark",
        "reason_max_wait_seconds": "reason_max_wait_seconds",
        "reason_target_open_intents": "reason_target_open_intents",
        "external_report_max_attempts": "external_report_max_attempts",
        "mozzie_base_url": "external_report_base_url",
        "mozzie_token": "external_report_token",
        "mozzie_timeout_seconds": "external_report_timeout_seconds",
        "guard_mode": "guard_mode",
        "guard_enabled_default": "guard_enabled_default",
        "guard_model_base_url": "guard_model_base_url",
        "guard_model_name": "guard_model_name",
        "guard_api_key": "guard_api_key",
        "guard_timeout_seconds": "guard_timeout_seconds",
    }
    values = {
        target: old[source_name]
        for source_name, target in field_map.items()
        if source_name in old
    }
    settings_rows = _rows(source, "settings")
    if settings_rows:
        settings = settings_rows[0]
        proxy_url = str(settings.get("proxy_url") or "").strip()
        if proxy_url:
            values["network_proxy_url"] = proxy_url
        if proxy_url and (
            bool(settings.get("proxy_bootstrap_enabled"))
            or bool(settings.get("proxy_explore_enabled"))
        ):
            values["network_proxy_mode"] = "http"
    try:
        config = RuntimeConfig.model_validate(values)
    except Exception as exc:
        report.warn(f"Runtime settings contained invalid values; defaults were used: {exc}")
        config = RuntimeConfig()
    conn.execute(
        runtime_settings.insert().values(
            id="runtime",
            config_json=config.model_dump_json(),
            version=max(1, int(source_row.get("version") or 1)),
            updated_by=str(source_row.get("updated_by") or "migration.legacy"),
            updated_at=source_row.get("updated_at") or now,
        )
    )
    report.add("runtime_settings")


def _insert_audit(conn, source, report) -> None:
    for row in _rows(source, "audit_events"):
        conn.execute(
            audit_events.insert().values(
                id=str(row.get("id")),
                project_id=row.get("project_id"),
                task_id=row.get("task_id"),
                intent_id=row.get("intent_id"),
                workspace_id=row.get("workspace_id"),
                worker=str(row.get("worker") or ""),
                event_type=str(row.get("event_type") or "legacy.event"),
                level=str(row.get("level") or "info"),
                message=str(row.get("message") or ""),
                payload_json=_json_text(row.get("payload_json"), "{}"),
                created_at=row.get("created_at") or utcnow(),
            )
        )
        report.add("audit_events")


def _insert_goal_evaluations(conn, source, task_ids, report, now) -> None:
    existing = {str(row.get("task_id")) for row in _rows(source, "goal_evaluations")}
    for task_id in sorted(task_ids - existing):
        task = source.execute("select status, completed_at, updated_at from tasks where id=?", (task_id,)).fetchone()
        if not task or task[0] != "completed":
            continue
        evaluation_id = f"legacy_goal_{task_id}"
        conn.execute(
            goal_evaluations.insert().values(
                id=evaluation_id,
                task_id=task_id,
                intent_id=None,
                workspace_id=None,
                achieved=True,
                reason="Legacy Task was marked completed; original Goal Evaluation was not persisted.",
                evidence_refs_json="[]",
                evaluator="migration.legacy",
                created_at=task[1] or task[2] or now,
            )
        )
        report.add("goal_evaluations")


def _rebuild_counters(conn, report) -> None:
    conn.execute(delete(id_counters))
    rows: list[dict[str, Any]] = []
    rows.extend(_counter_rows(conn, projects, "global", "project", "prj_"))
    rows.extend(_counter_rows(conn, tasks, "global", "task", "t_"))
    rows.extend(_counter_rows(conn, credentials, "global", "credential", "cred_"))
    rows.extend(_counter_rows(conn, reason_workspaces, "global", "reason_workspace", "rw_"))
    rows.extend(_counter_rows(conn, mcp_servers, "global", "mcp", "mcp_"))
    rows.extend(_counter_rows(conn, mcp_tools, "global", "mcp_tool", "mcp_tool_"))
    rows.extend(
        _counter_rows(
            conn,
            goal_evaluations,
            "global",
            "goal_evaluation",
            "ge",
        )
    )
    for task_id in _values(conn, tasks, "id"):
        for table, kind, prefix in (
            (facts, "fact", "f"),
            (intents, "intent", "i"),
            (task_hints, "hint", "h"),
            (todos, "todo", "td"),
            (artifacts, "artifact", "a"),
            (vulnerabilities, "vulnerability", "v"),
            (external_report_attempts, "external_report", "er"),
        ):
            rows.append({"scope": task_id, "kind": kind, "next_value": _next_numeric(conn, table, "id", prefix, task_id)})
        rows.append({"scope": task_id, "kind": "context_revision", "next_value": _next_numeric(conn, context_revisions, "revision", "", task_id)})
    for project_id in _values(conn, projects, "id"):
        rows.append({"scope": project_id, "kind": "knowledge", "next_value": _next_numeric(conn, project_knowledge, "id", "k", project_id)})
    conn.execute(id_counters.insert(), rows)
    report.add("id_counters", len(rows))


def _verify(engine: Engine, report: MigrationReport) -> None:
    with engine.connect() as conn:
        tables = (
            projects,
            tasks,
            facts,
            intents,
            task_hints,
            todos,
            artifacts,
            vulnerabilities,
            credentials,
            workers,
            audit_events,
            context_revisions,
            reason_workspaces,
            mcp_servers,
            mcp_tools,
            goal_evaluations,
        )
        checks = {
            table.name: int(
                conn.exec_driver_sql(f'SELECT COUNT(*) FROM "{table.name}"').scalar_one()
            )
            for table in tables
        }
        foreign_keys = [tuple(row) for row in conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall()]
        report.verification = {"counts": checks, "foreign_keys": foreign_keys}
        if foreign_keys:
            raise ValueError(f"Target foreign-key check failed: {foreign_keys[:5]}")


def _counter_rows(conn, table, scope, kind, prefix) -> list[dict[str, Any]]:
    value = _next_numeric(conn, table, "id", prefix, None)
    return [{"scope": scope, "kind": kind, "next_value": value}]


def _next_numeric(conn, table, column, prefix, scope) -> int:
    statement = select(table.c[column])
    if scope is not None and "task_id" in table.c:
        statement = statement.where(table.c.task_id == scope)
    if scope is not None and "project_id" in table.c and "task_id" not in table.c:
        statement = statement.where(table.c.project_id == scope)
    maximum = 0
    for value in conn.execute(statement).scalars():
        if column == "revision":
            try:
                maximum = max(maximum, int(value))
            except (TypeError, ValueError):
                continue
            continue
        text = str(value or "")
        match = re.fullmatch(rf"{re.escape(prefix)}(\d+)", text)
        if match:
            maximum = max(maximum, int(match.group(1)))
    return maximum + 1


def _values(conn, table, column) -> list[str]:
    return [str(value) for value in conn.execute(select(table.c[column])).scalars()]


def _materialize_file(value, source_root, target_root, report) -> tuple[str, str, int | None]:
    raw = str(value or "")
    source_path = Path(raw).expanduser()
    if not source_path.is_absolute():
        source_path = source_root / source_path
    source_path = source_path.resolve()
    try:
        relative = source_path.relative_to(source_root)
        storage_key = relative.as_posix()
    except ValueError:
        storage_key = f"legacy-import/{hashlib.sha256(raw.encode()).hexdigest()[:16]}/{source_path.name or 'missing'}"
        report.warn(f"Copied legacy file outside data root: {raw}")
    destination = target_root / Path(*storage_key.split("/"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source_path.is_file():
        if source_path.resolve() != destination.resolve():
            if destination.exists() and _sha256(destination) != _sha256(source_path):
                report.warn(f"Storage collision while copying {raw}; kept target copy")
            elif not destination.exists():
                shutil.copy2(source_path, destination)
        digest = _sha256(source_path)
        return storage_key, digest, source_path.stat().st_size
    marker = f"Legacy source file was missing during migration: {raw}\n"
    destination.write_text(marker, encoding="utf-8")
    report.warn(f"Missing legacy file materialized as a marker: {raw}")
    return storage_key, hashlib.sha256(marker.encode()).hexdigest(), len(marker.encode())


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _safe_legacy_draft(value) -> tuple[str, dict[str, Any] | None]:
    raw = _json_obj(value)
    try:
        from axiom.modules.reason.schemas import WorkspaceDraft

        return WorkspaceDraft.model_validate(raw).model_dump_json(), None
    except Exception:
        original = json.loads(json.dumps(raw, ensure_ascii=False))
        proposals = raw.get("intent_proposals") if isinstance(raw, dict) else None
        if isinstance(proposals, list):
            for proposal in proposals:
                if isinstance(proposal, dict):
                    description = str(proposal.get("description") or "Legacy intent proposal")
                    description = re.sub(r"execute_seconds|conclude_seconds|timeout|超时", "预算信息", description, flags=re.I)
                    proposal["description"] = description
        try:
            from axiom.modules.reason.schemas import WorkspaceDraft

            return WorkspaceDraft.model_validate(raw).model_dump_json(), original
        except Exception:
            return "{}", original


def _scope(value, enabled: bool) -> dict[str, Any]:
    raw = _json_obj(value)
    targets = raw.get("targets") if isinstance(raw.get("targets"), list) else []
    exclude = raw.get("exclude") if isinstance(raw.get("exclude"), list) else []
    return {
        "enabled": bool(raw.get("enabled", enabled)),
        "targets": [str(item).strip() for item in targets if str(item).strip()],
        "exclude": [str(item).strip() for item in exclude if str(item).strip()],
    }


def _intent_kind(row, intent_id: str) -> str:
    creator = str(row.get("creator") or "").lower()
    description = str(row.get("description") or "").lower()
    if intent_id == "i001" or "bootstrap" in creator or "dispatcher.asm" in creator or description == "bootstrap":
        return "bootstrap"
    return "explore"


def _credential_type(value) -> str:
    text = str(value or "custom").lower()
    if text in {"jwt", "bearer", "token"}:
        return "bearer"
    if text in {"cookie", "session"}:
        return "cookie"
    if text in {"basic"}:
        return "basic"
    if text in {"ssh"}:
        return "ssh"
    if text in {"header", "headers", "custom_header", "body"}:
        return "headers"
    return "custom"


def _rows(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    if not exists:
        return []
    return [dict(row) for row in conn.execute(f'SELECT * FROM "{table}"')]


def _json_obj(value) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if not value:
        return {}
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _json_list(value) -> list[Any]:
    if isinstance(value, list):
        return list(value)
    if not value:
        return []
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError):
        return []
    return list(parsed) if isinstance(parsed, list) else []


def _json_text(value, default: str) -> str:
    if value in (None, ""):
        return default
    if isinstance(value, str):
        try:
            json.loads(value)
            return value
        except json.JSONDecodeError:
            return json.dumps(value, ensure_ascii=False)
    return json.dumps(value, ensure_ascii=False)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
