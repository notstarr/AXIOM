"""Add an explicit routing kind to every Intent.

The dispatcher must route by a durable capability contract rather than infer
phase from free-form descriptions or creator names.
"""

from alembic import op
import sqlalchemy as sa


revision = "20260723_0002"
down_revision = "20260723_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("intents") as batch:
        batch.add_column(
            sa.Column("kind", sa.String(), nullable=False, server_default="explore")
        )
        batch.create_check_constraint(
            "ck_intents_kind",
            "kind IN ('bootstrap','reason','explore')",
        )
    op.execute("UPDATE intents SET kind = 'bootstrap' WHERE id = 'i001'")


def downgrade() -> None:
    with op.batch_alter_table("intents") as batch:
        batch.drop_constraint("ck_intents_kind", type_="check")
        batch.drop_column("kind")
