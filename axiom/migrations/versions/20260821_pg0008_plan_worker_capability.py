"""Register Plan as an independent Worker capability and phase scope."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

from axiom.shared.database import AXIOM_SCHEMA


revision = "20260821_pg0008"
down_revision = "20260821_pg0007"
branch_labels = None
depends_on = None


_PHASE_SCOPE_CHECK = "scope IN ('common','bootstrap','reason','explore','plan','review')"
_LEGACY_PHASE_SCOPE_CHECK = "scope IN ('common','bootstrap','reason','explore','review')"
_WORKER_PHASE_CHECK = "phase IN ('bootstrap','reason','explore','plan','review')"
_LEGACY_WORKER_PHASE_CHECK = "phase IN ('bootstrap','reason','explore','review')"


def _json_list(value) -> list[str]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return [item for item in parsed if isinstance(item, str)] if isinstance(parsed, list) else []


def _backfill_compatibility(connection) -> None:
    for row in connection.execute(
        sa.text(f'SELECT name, capabilities_json FROM "{AXIOM_SCHEMA}".workers')
    ).mappings():
        capabilities = _json_list(row["capabilities_json"])
        if "reason" not in capabilities or "plan" in capabilities:
            continue
        capabilities.append("plan")
        connection.execute(
            sa.text(
                f'UPDATE "{AXIOM_SCHEMA}".workers '
                'SET capabilities_json = CAST(:capabilities AS jsonb) WHERE name = :name'
            ),
            {"name": row["name"], "capabilities": json.dumps(capabilities, ensure_ascii=False)},
        )

    for row in connection.execute(
        sa.text(f'SELECT key, supported_phases_json FROM "{AXIOM_SCHEMA}".axiom_tools')
    ).mappings():
        phases = _json_list(row["supported_phases_json"])
        if "reason" not in phases or "plan" in phases:
            continue
        phases.append("plan")
        connection.execute(
            sa.text(
                f'UPDATE "{AXIOM_SCHEMA}".axiom_tools '
                'SET supported_phases_json = CAST(:phases AS jsonb) WHERE key = :key'
            ),
            {"key": row["key"], "phases": json.dumps(phases, ensure_ascii=False)},
        )

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    rows = list(connection.execute(sa.text(
        f'SELECT capability_kind, capability_key FROM "{AXIOM_SCHEMA}".phase_capability_bindings WHERE scope = \'reason\''
    )).mappings())
    for row in rows:
        connection.execute(sa.text(
            f'INSERT INTO "{AXIOM_SCHEMA}".phase_capability_bindings '
            "(scope, capability_kind, capability_key, created_at, updated_at) "
            "VALUES ('plan', :kind, :key, :now, :now) ON CONFLICT DO NOTHING"
        ), {"kind": row["capability_kind"], "key": row["capability_key"], "now": now})

    rows = list(connection.execute(sa.text(
        f'SELECT worker_name, tool_key FROM "{AXIOM_SCHEMA}".worker_axiom_tool_bindings WHERE phase = \'reason\''
    )).mappings())
    for row in rows:
        connection.execute(sa.text(
            f'INSERT INTO "{AXIOM_SCHEMA}".worker_axiom_tool_bindings '
            "(worker_name, phase, tool_key, created_at, updated_at) "
            "VALUES (:worker, 'plan', :tool, :now, :now) ON CONFLICT DO NOTHING"
        ), {"worker": row["worker_name"], "tool": row["tool_key"], "now": now})


def upgrade() -> None:
    op.execute(sa.text(
        f'ALTER TABLE "{AXIOM_SCHEMA}".phase_capability_bindings '
        'DROP CONSTRAINT IF EXISTS "ck_phase_capability_bindings_scope"'
    ))
    op.execute(sa.text(
        f'ALTER TABLE "{AXIOM_SCHEMA}".phase_capability_bindings '
        f'ADD CONSTRAINT "ck_phase_capability_bindings_scope" CHECK ({_PHASE_SCOPE_CHECK})'
    ))
    op.execute(sa.text(
        f'ALTER TABLE "{AXIOM_SCHEMA}".worker_axiom_tool_bindings '
        'DROP CONSTRAINT IF EXISTS "ck_worker_axiom_tool_bindings_phase"'
    ))
    op.execute(sa.text(
        f'ALTER TABLE "{AXIOM_SCHEMA}".worker_axiom_tool_bindings '
        f'ADD CONSTRAINT "ck_worker_axiom_tool_bindings_phase" CHECK ({_WORKER_PHASE_CHECK})'
    ))
    _backfill_compatibility(op.get_bind())


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text(f'DELETE FROM "{AXIOM_SCHEMA}".phase_capability_bindings WHERE scope = \'plan\''))
    connection.execute(sa.text(f'DELETE FROM "{AXIOM_SCHEMA}".worker_axiom_tool_bindings WHERE phase = \'plan\''))
    for row in connection.execute(sa.text(f'SELECT name, capabilities_json FROM "{AXIOM_SCHEMA}".workers')).mappings():
        capabilities = _json_list(row["capabilities_json"])
        if "plan" in capabilities:
            connection.execute(sa.text(
                f'UPDATE "{AXIOM_SCHEMA}".workers '
                'SET capabilities_json = CAST(:capabilities AS jsonb) WHERE name = :name'
            ), {
                "name": row["name"],
                "capabilities": json.dumps([item for item in capabilities if item != "plan"], ensure_ascii=False),
            })
    for row in connection.execute(sa.text(f'SELECT key, supported_phases_json FROM "{AXIOM_SCHEMA}".axiom_tools')).mappings():
        phases = _json_list(row["supported_phases_json"])
        if "plan" in phases:
            connection.execute(sa.text(
                f'UPDATE "{AXIOM_SCHEMA}".axiom_tools '
                'SET supported_phases_json = CAST(:phases AS jsonb) WHERE key = :key'
            ), {
                "key": row["key"],
                "phases": json.dumps([item for item in phases if item != "plan"], ensure_ascii=False),
            })
    op.execute(sa.text(f'ALTER TABLE "{AXIOM_SCHEMA}".phase_capability_bindings DROP CONSTRAINT IF EXISTS "ck_phase_capability_bindings_scope"'))
    op.execute(sa.text(f'ALTER TABLE "{AXIOM_SCHEMA}".phase_capability_bindings ADD CONSTRAINT "ck_phase_capability_bindings_scope" CHECK ({_LEGACY_PHASE_SCOPE_CHECK})'))
    op.execute(sa.text(f'ALTER TABLE "{AXIOM_SCHEMA}".worker_axiom_tool_bindings DROP CONSTRAINT IF EXISTS "ck_worker_axiom_tool_bindings_phase"'))
    op.execute(sa.text(f'ALTER TABLE "{AXIOM_SCHEMA}".worker_axiom_tool_bindings ADD CONSTRAINT "ck_worker_axiom_tool_bindings_phase" CHECK ({_LEGACY_WORKER_PHASE_CHECK})'))
