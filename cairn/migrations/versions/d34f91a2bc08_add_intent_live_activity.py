"""add Intent live activity fields

Revision ID: d34f91a2bc08
Revises: c20a11ef8207
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d34f91a2bc08"
down_revision: Union[str, Sequence[str], None] = "c20a11ef8207"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("intents", sa.Column("activity_kind", sa.String(), server_default="", nullable=False))
    op.add_column("intents", sa.Column("activity_summary", sa.Text(), server_default="", nullable=False))
    op.add_column("intents", sa.Column("activity_tool", sa.String(), server_default="", nullable=False))
    op.add_column("intents", sa.Column("activity_started_at", sa.String(), nullable=True))
    op.add_column("intents", sa.Column("activity_updated_at", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("intents", "activity_updated_at")
    op.drop_column("intents", "activity_started_at")
    op.drop_column("intents", "activity_tool")
    op.drop_column("intents", "activity_summary")
    op.drop_column("intents", "activity_kind")
