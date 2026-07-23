"""Create the durable worker configuration registry."""

from alembic import op
import sqlalchemy as sa


revision = "20260723_0003"
down_revision = "20260723_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workers",
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("worker_type", sa.String(), nullable=False),
        sa.Column("capabilities_json", sa.Text(), nullable=False),
        sa.Column("max_running", sa.Integer(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("config_json", sa.Text(), nullable=False),
        sa.Column("disabled", sa.Boolean(), nullable=False),
        sa.Column("cooldown_until", sa.String(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.CheckConstraint("max_running > 0", name="ck_workers_max_running"),
        sa.PrimaryKeyConstraint("name", name="pk_workers"),
    )


def downgrade() -> None:
    op.drop_table("workers")
