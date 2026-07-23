"""Add ownership tokens for Intent claim attempts."""

from alembic import op
import sqlalchemy as sa


revision = "20260723_0004"
down_revision = "20260723_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("intents") as batch:
        batch.add_column(sa.Column("lease_id", sa.String(), nullable=True))
        batch.add_column(sa.Column("claimed_at", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("intents") as batch:
        batch.drop_column("claimed_at")
        batch.drop_column("lease_id")
