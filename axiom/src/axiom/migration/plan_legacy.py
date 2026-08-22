"""Portable migration helpers for legacy ``reason(trigger=plan)`` rows.

The helper deliberately reflects tables from the migration connection.  The
application metadata uses the PostgreSQL ``axiom`` schema, while the generic
SQLite migration tests use ordinary unqualified table names.
"""

from __future__ import annotations

import json

import sqlalchemy as sa


def _table(bind, metadata, name: str, schema: str | None):
    return sa.Table(name, metadata, autoload_with=bind, schema=schema)


def _json(value) -> dict:
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _dump(bind, value):
    return value if bind.dialect.name == "postgresql" else json.dumps(value, ensure_ascii=False)


def migrate_legacy_plan_rows(bind, *, schema: str | None = None) -> int:
    metadata = sa.MetaData()
    reason_runs = _table(bind, metadata, "reason_runs", schema)
    reason_sources = _table(bind, metadata, "reason_run_sources", schema)
    reason_workspaces = _table(bind, metadata, "reason_workspaces", schema)
    plan_runs = _table(bind, metadata, "plan_runs", schema)
    plan_sources = _table(bind, metadata, "plan_run_sources", schema)
    plan_workspaces = _table(bind, metadata, "plan_workspaces", schema)
    context_revisions = _table(bind, metadata, "context_revisions", schema)
    audit_events = _table(bind, metadata, "audit_events", schema)
    copied = 0
    rows = bind.execute(
        sa.select(reason_runs).where(reason_runs.c.trigger == "plan")
    ).mappings().all()
    for row in rows:
        if bind.execute(
            sa.select(plan_runs.c.id).where(plan_runs.c.id == row["id"])
        ).scalar_one_or_none() is not None:
            continue
        payload = _json(row.get("trigger_payload_json"))
        original_status = row["status"]
        is_open = original_status in {"pending", "claimed", "running", "concluding"}
        payload.update(
            {
                "legacy_reason_run_id": row["id"],
                "migrated_to_plan_runtime": True,
                **(
                    {"legacy_status_before_migration": original_status}
                    if is_open
                    else {}
                ),
            }
        )
        source_ids = list(
            bind.execute(
                sa.select(reason_sources.c.fact_id).where(
                    reason_sources.c.task_id == row["task_id"],
                    reason_sources.c.reason_run_id == row["id"],
                )
            ).scalars()
        )
        values = {
            "id": row["id"],
            "task_id": row["task_id"],
            "goal_cycle_id": row.get("goal_cycle_id"),
            "status": "pending" if is_open else original_status,
            "trigger": "legacy_migration",
            "trigger_payload_json": _dump(bind, payload),
            "input_fingerprint": row["input_fingerprint"],
            "worker_pool_json": row["worker_pool_json"],
            "worker": None if is_open else row.get("worker"),
            "settings_version": row["settings_version"],
            "execute_timeout_seconds": row["execute_timeout_seconds"],
            "conclude_timeout_seconds": row["conclude_timeout_seconds"],
            "lease_id": None if is_open else row.get("lease_id"),
            "claimed_at": None if is_open else row.get("claimed_at"),
            "last_heartbeat_at": None if is_open else row.get("last_heartbeat_at"),
            "retry_count": row.get("retry_count", 0),
            "infrastructure_retry_count": row.get("infrastructure_retry_count", 0),
            "next_retry_at": None if is_open else row.get("next_retry_at"),
            "last_failure_kind": row.get("last_failure_kind") or ("legacy_plan_migrated" if is_open else ""),
            "failure_class": row.get("failure_class") or ("migration" if is_open else ""),
            "activity_kind": "pending" if is_open else row.get("activity_kind", ""),
            "activity_summary": "Legacy Reason Plan 已迁移，等待独立 Plan Worker" if is_open else row.get("activity_summary", ""),
            "activity_tool": row.get("activity_tool", ""),
            "activity_started_at": None if is_open else row.get("activity_started_at"),
            "activity_updated_at": row.get("activity_updated_at"),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "concluded_at": None if is_open else row.get("concluded_at"),
            "decision_json": _dump(bind, payload.get("decision") or {}),
        }
        bind.execute(plan_runs.insert().values(**values))
        if source_ids:
            bind.execute(
                plan_sources.insert(),
                [
                    {"task_id": row["task_id"], "plan_run_id": row["id"], "fact_id": fact_id}
                    for fact_id in source_ids
                ],
            )
        workspaces = bind.execute(
            sa.select(reason_workspaces).where(
                reason_workspaces.c.task_id == row["task_id"],
                reason_workspaces.c.reason_run_id == row["id"],
            )
        ).mappings().all()
        for workspace in workspaces:
            status = "abandoned" if workspace["status"] == "open" else workspace["status"]
            bind.execute(
                plan_workspaces.insert().values(
                    id=workspace["id"],
                    task_id=workspace["task_id"],
                    plan_run_id=row["id"],
                    lease_id=workspace["lease_id"],
                    status=status,
                    context_revision=workspace["context_revision"],
                    draft_json=workspace["draft_json"],
                    worker=workspace["worker"],
                    failure_reason=("legacy Reason Plan workspace migrated as read-only" if status == "abandoned" else workspace["failure_reason"]),
                    result_json=workspace["result_json"],
                    decision_json=_dump(bind, payload.get("decision") or {}),
                    created_at=workspace["created_at"],
                    updated_at=workspace["updated_at"],
                    committed_at=workspace["committed_at"],
                )
            )
        if is_open:
            bind.execute(
                reason_runs.update()
                .where(reason_runs.c.task_id == row["task_id"], reason_runs.c.id == row["id"])
                .values(
                    status="cancelled",
                    worker=None,
                    lease_id=None,
                    claimed_at=None,
                    last_heartbeat_at=None,
                    next_retry_at=None,
                    last_failure_kind="legacy_plan_migrated",
                    failure_class="migration",
                    activity_kind="legacy_plan_migrated",
                    activity_summary="Legacy Plan 已迁移到独立 PlanRun，只读保留",
                    concluded_at=row.get("concluded_at"),
                )
            )
        bind.execute(
            context_revisions.update()
            .where(
                context_revisions.c.task_id == row["task_id"],
                context_revisions.c.reason_run_id == row["id"],
            )
            .values(reason_run_id=None, plan_run_id=row["id"])
        )
        bind.execute(
            audit_events.update()
            .where(
                audit_events.c.task_id == row["task_id"],
                audit_events.c.reason_run_id == row["id"],
            )
            .values(reason_run_id=None, plan_run_id=row["id"])
        )
        copied += 1
    return copied


def restore_plan_rows_for_downgrade(bind, *, schema: str | None = None) -> int:
    metadata = sa.MetaData()
    reason_runs = _table(bind, metadata, "reason_runs", schema)
    reason_sources = _table(bind, metadata, "reason_run_sources", schema)
    reason_workspaces = _table(bind, metadata, "reason_workspaces", schema)
    plan_runs = _table(bind, metadata, "plan_runs", schema)
    plan_sources = _table(bind, metadata, "plan_run_sources", schema)
    plan_workspaces = _table(bind, metadata, "plan_workspaces", schema)
    context_revisions = _table(bind, metadata, "context_revisions", schema)
    audit_events = _table(bind, metadata, "audit_events", schema)
    restored = 0
    for row in bind.execute(sa.select(plan_runs)).mappings().all():
        payload = _json(row.get("trigger_payload_json"))
        original_status = payload.get("legacy_status_before_migration")
        existing = bind.execute(
            sa.select(reason_runs.c.id).where(reason_runs.c.id == row["id"])
        ).scalar_one_or_none()
        if existing is None:
            values = {
                "id": row["id"],
                "task_id": row["task_id"],
                "goal_cycle_id": row.get("goal_cycle_id"),
                "status": row["status"],
                "trigger": "plan",
                "trigger_payload_json": row["trigger_payload_json"],
                "input_fingerprint": row["input_fingerprint"],
                "worker_pool_json": row["worker_pool_json"],
                "worker": row.get("worker"),
                "settings_version": row["settings_version"],
                "execute_timeout_seconds": row["execute_timeout_seconds"],
                "conclude_timeout_seconds": row["conclude_timeout_seconds"],
                "target_open_intents": 0,
                "max_new_intents": 0,
                "explore_execute_min_seconds": 0,
                "explore_execute_max_seconds": 0,
                "explore_conclude_min_seconds": 0,
                "explore_conclude_max_seconds": 0,
                "lease_id": row.get("lease_id"),
                "claimed_at": row.get("claimed_at"),
                "last_heartbeat_at": row.get("last_heartbeat_at"),
                "retry_count": row.get("retry_count", 0),
                "infrastructure_retry_count": row.get("infrastructure_retry_count", 0),
                "next_retry_at": row.get("next_retry_at"),
                "last_failure_kind": row.get("last_failure_kind", ""),
                "failure_class": row.get("failure_class", ""),
                "activity_kind": row.get("activity_kind", ""),
                "activity_summary": row.get("activity_summary", ""),
                "activity_tool": row.get("activity_tool", ""),
                "activity_started_at": row.get("activity_started_at"),
                "activity_updated_at": row.get("activity_updated_at"),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "concluded_at": row.get("concluded_at"),
            }
            bind.execute(reason_runs.insert().values(**values))
        elif original_status:
            bind.execute(
                reason_runs.update()
                .where(reason_runs.c.id == row["id"])
                .values(status=original_status)
            )
        for source in bind.execute(
            sa.select(plan_sources).where(plan_sources.c.plan_run_id == row["id"])
        ).mappings():
            exists_source = bind.execute(
                sa.select(reason_sources.c.reason_run_id).where(
                    reason_sources.c.task_id == source["task_id"],
                    reason_sources.c.reason_run_id == row["id"],
                    reason_sources.c.fact_id == source["fact_id"],
                )
            ).scalar_one_or_none()
            if exists_source is None:
                bind.execute(
                    reason_sources.insert().values(
                        task_id=source["task_id"],
                        reason_run_id=row["id"],
                        fact_id=source["fact_id"],
                    )
                )
        for workspace in bind.execute(
            sa.select(plan_workspaces).where(plan_workspaces.c.plan_run_id == row["id"])
        ).mappings():
            if bind.execute(sa.select(reason_workspaces.c.id).where(reason_workspaces.c.id == workspace["id"])).scalar_one_or_none() is not None:
                continue
            bind.execute(
                reason_workspaces.insert().values(
                    id=workspace["id"],
                    task_id=workspace["task_id"],
                    intent_id=None,
                    reason_run_id=row["id"],
                    lease_id=workspace["lease_id"],
                    status=workspace["status"],
                    context_revision=workspace["context_revision"],
                    draft_json=workspace["draft_json"],
                    worker=workspace["worker"],
                    failure_reason=workspace["failure_reason"],
                    result_json=workspace["result_json"],
                    created_at=workspace["created_at"],
                    updated_at=workspace["updated_at"],
                    committed_at=workspace["committed_at"],
                )
            )
        restored += 1
    bind.execute(
        context_revisions.update()
        .where(context_revisions.c.plan_run_id.is_not(None))
        .values(reason_run_id=context_revisions.c.plan_run_id, plan_run_id=None)
    )
    bind.execute(
        audit_events.update()
        .where(audit_events.c.plan_run_id.is_not(None))
        .values(reason_run_id=audit_events.c.plan_run_id, plan_run_id=None)
    )
    return restored
