"""split intent execution and infrastructure retry accounting

Revision ID: 4e8a1f9c2b77
Revises: f62b8d1e4a90
Create Date: 2026-07-22 16:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "4e8a1f9c2b77"
down_revision: Union[str, Sequence[str], None] = "f62b8d1e4a90"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("intents") as batch_op:
        batch_op.add_column(
            sa.Column("infrastructure_retry_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("failure_class", sa.String(), nullable=False, server_default="")
        )


def downgrade() -> None:
    with op.batch_alter_table("intents") as batch_op:
        batch_op.drop_column("failure_class")
        batch_op.drop_column("infrastructure_retry_count")
