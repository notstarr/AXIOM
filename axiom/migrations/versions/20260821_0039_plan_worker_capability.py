"""Register Plan as an independent Worker capability and phase scope."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op


revision = "20260821_0039"
down_revision = "20260821_0038"
branch_labels = None
depends_on = None


_PHASE_SCOPE_CHECK = "scope IN ('common','bootstrap','reason','explore','plan','review')"
_LEGACY_PHASE_SCOPE_CHECK = "scope IN ('common','bootstrap','reason','explore','review')"
_WORKER_PHASE_CHECK = "phase IN ('bootstrap','reason','explore','plan','review')"
_LEGACY_WORKER_PHASE_CHECK = "phase IN ('bootstrap','reason','explore','review')"


def _replace_sqlite_checks(bind, table: str, marker: str, constraint_name: str, expression: str) -> None:
    with op.batch_alter_table(table, recreate="always") as batch:
        for constraint in sa.inspect(bind).get_check_constraints(table):
            sqltext = str(constraint.get("sqltext", ""))
            if marker in sqltext:
                name = constraint.get("name")
                if name:
                    batch.drop_constraint(name, type="check")
        batch.create_check_constraint(constraint_name, expression)


def _backfill_compatibility(bind) -> None:
    connection = bind

    # Existing model backends which could run Reason are safe defaults for the
    # new Plan pool.  Users can remove this inherited capability afterwards.
    for row in connection.execute(sa.text("SELECT name, capabilities_json FROM workers")).mappings():
        try:
            capabilities = json.loads(row["capabilities_json"] or "[]")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(capabilities, list) or "reason" not in capabilities or "plan" in capabilities:
            continue
        capabilities.append("plan")
        connection.execute(
            sa.text("UPDATE workers SET capabilities_json = :capabilities WHERE name = :name"),
            {"name": row["name"], "capabilities": json.dumps(capabilities, ensure_ascii=False)},
        )

    # A tool manifest that supported Reason before this migration inherits
    # Plan support.  This makes the default backend/capability copy usable
    # without requiring every existing package to be rescanned manually.
    for row in connection.execute(
        sa.text("SELECT key, supported_phases_json FROM axiom_tools")
    ).mappings():
        try:
            phases = json.loads(row["supported_phases_json"] or "[]")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(phases, list) or "reason" not in phases or "plan" in phases:
            continue
        phases.append("plan")
        connection.execute(
            sa.text("UPDATE axiom_tools SET supported_phases_json = :phases WHERE key = :key"),
            {"key": row["key"], "phases": json.dumps(phases, ensure_ascii=False)},
        )

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    # The phase-capability matrix is the runtime source of truth.  Copying
    # Reason bindings gives existing installations a safe Plan default while
    # preserving explicit Plan-only configuration on newer installations.
    reason_rows = list(
        connection.execute(
            sa.text(
                "SELECT capability_kind, capability_key FROM phase_capability_bindings "
                "WHERE scope = 'reason'"
            )
        ).mappings()
    )
    for row in reason_rows:
        exists = connection.execute(
            sa.text(
                "SELECT 1 FROM phase_capability_bindings WHERE scope = 'plan' "
                "AND capability_kind = :kind AND capability_key = :key"
            ),
            {"kind": row["capability_kind"], "key": row["capability_key"]},
        ).first()
        if exists:
            continue
        connection.execute(
            sa.text(
                "INSERT INTO phase_capability_bindings "
                "(scope, capability_kind, capability_key, created_at, updated_at) "
                "VALUES ('plan', :kind, :key, :now, :now)"
            ),
            {"kind": row["capability_kind"], "key": row["capability_key"], "now": now},
        )

    # Keep the retired worker-specific table coherent for old API readers.
    reason_rows = list(
        connection.execute(
            sa.text(
                "SELECT worker_name, tool_key FROM worker_axiom_tool_bindings "
                "WHERE phase = 'reason'"
            )
        ).mappings()
    )
    for row in reason_rows:
        exists = connection.execute(
            sa.text(
                "SELECT 1 FROM worker_axiom_tool_bindings WHERE worker_name = :worker "
                "AND phase = 'plan' AND tool_key = :tool"
            ),
            {"worker": row["worker_name"], "tool": row["tool_key"]},
        ).first()
        if exists:
            continue
        connection.execute(
            sa.text(
                "INSERT INTO worker_axiom_tool_bindings "
                "(worker_name, phase, tool_key, created_at, updated_at) "
                "VALUES (:worker, 'plan', :tool, :now, :now)"
            ),
            {"worker": row["worker_name"], "tool": row["tool_key"], "now": now},
        )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        _replace_sqlite_checks(
            bind,
            "phase_capability_bindings",
            "scope IN",
            "ck_phase_capability_bindings_scope",
            _PHASE_SCOPE_CHECK,
        )
        _replace_sqlite_checks(
            bind,
            "worker_axiom_tool_bindings",
            "phase IN",
            "ck_worker_axiom_tool_bindings_phase",
            _WORKER_PHASE_CHECK,
        )
    else:
        op.execute('ALTER TABLE "phase_capability_bindings" DROP CONSTRAINT IF EXISTS "ck_phase_capability_bindings_scope"')
        op.execute('ALTER TABLE "phase_capability_bindings" ADD CONSTRAINT "ck_phase_capability_bindings_scope" CHECK (' + _PHASE_SCOPE_CHECK + ')')
        op.execute('ALTER TABLE "worker_axiom_tool_bindings" DROP CONSTRAINT IF EXISTS "ck_worker_axiom_tool_bindings_phase"')
        op.execute('ALTER TABLE "worker_axiom_tool_bindings" ADD CONSTRAINT "ck_worker_axiom_tool_bindings_phase" CHECK (' + _WORKER_PHASE_CHECK + ')')
    _backfill_compatibility(bind)


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("DELETE FROM phase_capability_bindings WHERE scope = 'plan'"))
    bind.execute(sa.text("DELETE FROM worker_axiom_tool_bindings WHERE phase = 'plan'"))
    for row in bind.execute(sa.text("SELECT name, capabilities_json FROM workers")).mappings():
        try:
            capabilities = json.loads(row["capabilities_json"] or "[]")
        except (TypeError, json.JSONDecodeError):
            continue
        if "plan" not in capabilities:
            continue
        bind.execute(
            sa.text("UPDATE workers SET capabilities_json = :capabilities WHERE name = :name"),
            {
                "name": row["name"],
                "capabilities": json.dumps([item for item in capabilities if item != "plan"], ensure_ascii=False),
            },
        )
    for row in bind.execute(sa.text("SELECT key, supported_phases_json FROM axiom_tools")).mappings():
        try:
            phases = json.loads(row["supported_phases_json"] or "[]")
        except (TypeError, json.JSONDecodeError):
            continue
        if "plan" not in phases:
            continue
        bind.execute(
            sa.text("UPDATE axiom_tools SET supported_phases_json = :phases WHERE key = :key"),
            {"key": row["key"], "phases": json.dumps([item for item in phases if item != "plan"], ensure_ascii=False)},
        )
    if bind.dialect.name == "sqlite":
        _replace_sqlite_checks(
            bind,
            "phase_capability_bindings",
            "scope IN",
            "ck_phase_capability_bindings_scope",
            _LEGACY_PHASE_SCOPE_CHECK,
        )
        _replace_sqlite_checks(
            bind,
            "worker_axiom_tool_bindings",
            "phase IN",
            "ck_worker_axiom_tool_bindings_phase",
            _LEGACY_WORKER_PHASE_CHECK,
        )
    else:
        op.execute('ALTER TABLE "phase_capability_bindings" DROP CONSTRAINT IF EXISTS "ck_phase_capability_bindings_scope"')
        op.execute('ALTER TABLE "phase_capability_bindings" ADD CONSTRAINT "ck_phase_capability_bindings_scope" CHECK (' + _LEGACY_PHASE_SCOPE_CHECK + ')')
        op.execute('ALTER TABLE "worker_axiom_tool_bindings" DROP CONSTRAINT IF EXISTS "ck_worker_axiom_tool_bindings_phase"')
        op.execute('ALTER TABLE "worker_axiom_tool_bindings" ADD CONSTRAINT "ck_worker_axiom_tool_bindings_phase" CHECK (' + _LEGACY_WORKER_PHASE_CHECK + ')')
