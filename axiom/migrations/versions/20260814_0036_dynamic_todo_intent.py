"""Add canonical Todo lifecycle and durable Intent assessment fields."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260814_0036"
down_revision = "20260805_0035"
branch_labels = None
depends_on = None


_TODO_STATUS_TRANSITION = (
    "status IN ('pending','active','open','partial','blocked','completed','rejected','obsolete')"
)
_TODO_STATUS_CANONICAL = "status IN ('open','partial','blocked','completed','rejected','obsolete')"
_TODO_STATUS_LEGACY = "status IN ('pending','active','completed','rejected','obsolete')"


def _replace_todo_status_check(expression: str, *, name: str = "ck_todos_status") -> None:
    """Replace the Todo status check on both legacy SQLite and PG branches.

    The generic revision is primarily used to describe/export the historical
    SQLite schema. SQLite cannot alter a CHECK in place, and updating a row
    while the old CHECK is still present fails immediately. Rebuilding the
    table through Alembic's batch operation keeps the existing foreign keys,
    indexes, and data while allowing the status values to be migrated safely.
    """

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    status_constraints = [
        item["name"]
        for item in inspector.get_check_constraints("todos")
        if item.get("name")
        and "status" in str(item.get("sqltext") or "").lower()
    ]
    if bind.dialect.name == "sqlite":
        # Rebuilding the parent table with SQLite foreign keys enabled causes
        # its ON DELETE CASCADE child (todo_sources) to be emptied. Keep that
        # child data in a temporary table and restore it after the rebuild.
        backup_table = "_axiom_todo_sources_status_backup"
        preserve_sources = inspector.has_table("todo_sources")
        if preserve_sources:
            bind.exec_driver_sql(f'DROP TABLE IF EXISTS "{backup_table}"')
            bind.exec_driver_sql(
                f'CREATE TEMP TABLE "{backup_table}" AS '
                'SELECT task_id, todo_id, source_type, source_id FROM todo_sources'
            )
        try:
            with op.batch_alter_table("todos", recreate="always") as batch:
                for constraint_name in status_constraints:
                    batch.drop_constraint(constraint_name, type_="check")
                batch.create_check_constraint(name, expression)
        except BaseException:
            if preserve_sources:
                bind.exec_driver_sql(f'DROP TABLE IF EXISTS "{backup_table}"')
            raise
        if preserve_sources:
            bind.exec_driver_sql("DELETE FROM todo_sources")
            bind.exec_driver_sql(
                f'INSERT INTO todo_sources (task_id, todo_id, source_type, source_id) '
                f'SELECT task_id, todo_id, source_type, source_id FROM "{backup_table}"'
            )
            bind.exec_driver_sql(f'DROP TABLE IF EXISTS "{backup_table}"')
        return

    for constraint_name in status_constraints:
        op.drop_constraint(constraint_name, "todos", type_="check")
    op.create_check_constraint(name, "todos", expression)


def upgrade() -> None:
    # The SQLite branch is retained for exported/legacy databases.  Runtime
    # migrations are PostgreSQL-only, but keeping this branch structurally
    # equivalent makes the frozen import contract and schema graph complete.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {item["name"] for item in inspector.get_columns("intents")}
    additions = (
        ("priority", sa.Column("priority", sa.Integer(), nullable=False, server_default="50")),
        ("assessment_status", sa.Column("assessment_status", sa.String(), nullable=False, server_default="open")),
        ("assessment_reason", sa.Column("assessment_reason", sa.Text(), nullable=False, server_default="")),
        ("assessed_at", sa.Column("assessed_at", sa.String(), nullable=True)),
    )
    for name, column in additions:
        if name not in existing:
            op.add_column("intents", column)

    # First install a transitional check so legacy rows can be rewritten on
    # SQLite, where the old CHECK cannot be dropped in place.
    _replace_todo_status_check(_TODO_STATUS_TRANSITION, name="ck_todos_status_transition")
    op.execute("UPDATE todos SET status = 'open' WHERE status = 'pending'")
    op.execute("UPDATE todos SET status = 'partial' WHERE status = 'active'")
    op.execute("UPDATE todos SET completed_at = NULL WHERE status <> 'completed'")
    op.execute(
        "UPDATE todos SET status_reason = '从旧 Todo 状态迁移' "
        "WHERE status IN ('partial','blocked','completed') "
        "AND trim(COALESCE(status_reason, '')) = ''"
    )
    _replace_todo_status_check(_TODO_STATUS_CANONICAL)


def downgrade() -> None:
    _replace_todo_status_check(_TODO_STATUS_TRANSITION, name="ck_todos_status_transition")
    op.execute("UPDATE todos SET status = 'pending' WHERE status = 'open'")
    op.execute("UPDATE todos SET status = 'active' WHERE status IN ('partial','blocked')")
    op.execute("UPDATE todos SET completed_at = NULL WHERE status <> 'completed'")
    _replace_todo_status_check(_TODO_STATUS_LEGACY)
    bind = op.get_bind()
    existing = {item["name"] for item in sa.inspect(bind).get_columns("intents")}
    for name in ("assessed_at", "assessment_reason", "assessment_status", "priority"):
        if name in existing:
            op.drop_column("intents", name)
