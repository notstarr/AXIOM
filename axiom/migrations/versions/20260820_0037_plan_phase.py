"""Allow Tasks to enter the Plan panorama phase on the generic/SQLite branch."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260820_0037"
down_revision = "20260814_0036"
branch_labels = None
depends_on = None


_PHASE_CHECK = "phase IN ('bootstrap','reason','explore','plan','waiting','none')"
_LEGACY_PHASE_CHECK = "phase IN ('bootstrap','reason','explore','waiting','none')"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("tasks", recreate="always") as batch:
            for constraint in sa.inspect(bind).get_check_constraints("tasks"):
                if constraint.get("name") and "phase" in str(constraint.get("sqltext", "")):
                    batch.drop_constraint(constraint["name"], type_="check")
            batch.create_check_constraint("ck_tasks_phase", _PHASE_CHECK)
        return
    op.execute('ALTER TABLE "tasks" DROP CONSTRAINT IF EXISTS "ck_tasks_phase"')
    op.execute(
        'ALTER TABLE "tasks" ADD CONSTRAINT "ck_tasks_phase" '
        "CHECK (phase IN ('bootstrap','reason','explore','plan','waiting','none'))"
    )


def downgrade() -> None:
    bind = op.get_bind()
    op.execute("UPDATE tasks SET phase = 'waiting' WHERE phase = 'plan'")
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("tasks", recreate="always") as batch:
            for constraint in sa.inspect(bind).get_check_constraints("tasks"):
                if constraint.get("name") and "phase" in str(constraint.get("sqltext", "")):
                    batch.drop_constraint(constraint["name"], type_="check")
            batch.create_check_constraint("ck_tasks_phase", _LEGACY_PHASE_CHECK)
        return
    op.execute('ALTER TABLE "tasks" DROP CONSTRAINT IF EXISTS "ck_tasks_phase"')
    op.execute(
        'ALTER TABLE "tasks" ADD CONSTRAINT "ck_tasks_phase" '
        "CHECK (phase IN ('bootstrap','reason','explore','waiting','none'))"
    )
