"""Add worker directives and one-shot delivery ledger (PostgreSQL branch).

``tables.py`` uses ``UTCDateTimeText`` (TIMESTAMPTZ under PostgreSQL) and Python-side
``default=`` only. This migration must produce the same column types, the same
constraint names and the same indexes, or ``compare_metadata`` reports drift.
The ``has_table`` guard matters because ``pg0001`` builds from live metadata on a
fresh database, so the table may already exist when this revision runs.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from axiom.shared.database import AXIOM_SCHEMA


revision = "20261106_pg0034"
down_revision = "20261106_pg0033"
branch_labels = None
depends_on = None


_DIRECTIVE_CHECKS = (
    ("ck_worker_directives_scope", "scope IN ('reason_all','reason_next','explore_all','explore_one','all_workers')"),
    ("ck_worker_directives_lifetime", "lifetime IN ('persistent','once')"),
    ("ck_worker_directives_status", "status IN ('pending','active','delivered','disabled','expired')"),
    (
        "ck_worker_directives_target",
        "(scope = 'explore_one' AND target_intent_id IS NOT NULL) "
        "OR (scope <> 'explore_one' AND target_intent_id IS NULL)",
    ),
    (
        "ck_worker_directives_consistency",
        "(lifetime = 'persistent' AND scope IN ('reason_all','explore_all','all_workers')) "
        "OR (lifetime = 'once' AND scope IN ('reason_next','explore_one'))",
    ),
)
_DELIVERY_CHECKS = (
    ("ck_directive_deliveries_delivery_kind", "delivery_kind IN ('reason_next','explore_one')"),
    ("ck_directive_deliveries_status", "status IN ('queued','delivered','reopened')"),
)
_DIRECTIVE_INDEXES = (
    ("uq_worker_directives_client", ["task_id", "client_message_id"], True),
    ("ix_worker_directives_task_scope_status", ["task_id", "scope", "status"], False),
    ("ix_worker_directives_task_status_created", ["task_id", "status", "created_at", "id"], False),
    ("ix_worker_directives_task_target_status", ["task_id", "target_intent_id", "status"], False),
)
_DELIVERY_INDEXES = (
    ("uq_directive_deliveries_lease", ["directive_id", "lease_id"], True),
    ("uq_directive_deliveries_reason", ["delivery_kind", "reason_run_id", "directive_id"], True),
    ("ix_directive_deliveries_lease", ["lease_id", "status"], False),
    ("ix_directive_deliveries_run", ["task_id", "reason_run_id"], False),
    ("ix_directive_deliveries_intent", ["task_id", "intent_id", "status"], False),
)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    schema = AXIOM_SCHEMA
    if not inspector.has_table("worker_directives", schema=schema):
        checks = [sa.CheckConstraint(sql, name=op.f(name)) for name, sql in _DIRECTIVE_CHECKS]
        op.create_table(
            "worker_directives",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("task_id", sa.String(), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("creator", sa.String(), nullable=False),
            sa.Column("scope", sa.String(), nullable=False),
            sa.Column("lifetime", sa.String(), nullable=False),
            sa.Column("target_intent_id", sa.String()),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("client_message_id", sa.String()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
            sa.Column("delivered_at", sa.DateTime(timezone=True)),
            sa.Column("delivered_run_id", sa.String()),
            sa.Column("disabled_at", sa.DateTime(timezone=True)),
            sa.Column("expires_at", sa.DateTime(timezone=True)),
            sa.Column("archived", sa.Boolean(), nullable=False),
            sa.PrimaryKeyConstraint("task_id", "id", name="pk_worker_directives"),
            sa.ForeignKeyConstraint(
                ["task_id"], ["axiom.tasks.id"],
                name="fk_worker_directives_task_id_tasks", ondelete="CASCADE",
            ),
            *checks,
            schema=schema,
        )
        for name, columns, unique in _DIRECTIVE_INDEXES:
            op.create_index(name, "worker_directives", columns, unique=unique, schema=schema)

    if not inspector.has_table("directive_deliveries", schema=schema):
        checks = [sa.CheckConstraint(sql, name=op.f(name)) for name, sql in _DELIVERY_CHECKS]
        op.create_table(
            "directive_deliveries",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("task_id", sa.String(), nullable=False),
            sa.Column("directive_id", sa.String(), nullable=False),
            sa.Column("delivery_kind", sa.String(), nullable=False),
            sa.Column("reason_run_id", sa.String()),
            sa.Column("intent_id", sa.String()),
            sa.Column("lease_id", sa.String()),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("delivered_at", sa.DateTime(timezone=True)),
            sa.Column("reopened_at", sa.DateTime(timezone=True)),
            sa.PrimaryKeyConstraint("task_id", "id", name="pk_directive_deliveries"),
            *checks,
            schema=schema,
        )
        for name, columns, unique in _DELIVERY_INDEXES:
            op.create_index(name, "directive_deliveries", columns, unique=unique, schema=schema)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    schema = AXIOM_SCHEMA
    if inspector.has_table("directive_deliveries", schema=schema):
        for name, _columns, _unique in _DELIVERY_INDEXES:
            op.drop_index(name, table_name="directive_deliveries", schema=schema)
        op.drop_table("directive_deliveries", schema=schema)
    if inspector.has_table("worker_directives", schema=schema):
        for name, _columns, _unique in _DIRECTIVE_INDEXES:
            op.drop_index(name, table_name="worker_directives", schema=schema)
        op.drop_table("worker_directives", schema=schema)
