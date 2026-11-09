"""Add worker directives and one-shot delivery ledger (generic/SQLite branch).

Column types, constraint names and indexes must match ``directives/tables.py``
exactly: the PostgreSQL chain is checked by ``compare_metadata`` with
``compare_server_default=True``, and the baseline migration builds tables from
the shared metadata. No ``server_default`` is declared here because the table
definitions use Python-side ``default=`` only.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from axiom.shared.database import AXIOM_SCHEMA


revision = "20261106_0058"
down_revision = "20261106_0057"
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


def _time():
    return sa.String()


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
            sa.Column("created_at", _time(), nullable=False),
            sa.Column("updated_at", _time()),
            sa.Column("delivered_at", _time()),
            sa.Column("delivered_run_id", sa.String()),
            sa.Column("disabled_at", _time()),
            sa.Column("expires_at", _time()),
            sa.Column("archived", sa.Boolean(), nullable=False),
            sa.PrimaryKeyConstraint("task_id", "id", name="pk_worker_directives"),
            sa.ForeignKeyConstraint(
                ["task_id"], ["tasks.id"],
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
            sa.Column("created_at", _time(), nullable=False),
            sa.Column("delivered_at", _time()),
            sa.Column("reopened_at", _time()),
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
