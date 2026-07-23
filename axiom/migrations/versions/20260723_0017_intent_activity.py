"""Persist human-readable current Intent activity."""

from alembic import op
import sqlalchemy as sa


revision = "20260723_0017"
down_revision = "20260723_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("intents") as batch:
        batch.add_column(sa.Column("activity_kind", sa.String(), nullable=False, server_default=""))
        batch.add_column(sa.Column("activity_summary", sa.Text(), nullable=False, server_default=""))
        batch.add_column(sa.Column("activity_tool", sa.String(), nullable=False, server_default=""))
        batch.add_column(sa.Column("activity_started_at", sa.String(), nullable=True))
        batch.add_column(sa.Column("activity_updated_at", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("intents") as batch:
        batch.drop_column("activity_updated_at")
        batch.drop_column("activity_started_at")
        batch.drop_column("activity_tool")
        batch.drop_column("activity_summary")
        batch.drop_column("activity_kind")
