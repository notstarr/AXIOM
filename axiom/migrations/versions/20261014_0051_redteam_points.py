"""Add red-team result scoring columns for the 100-point objective."""
from alembic import op
import sqlalchemy as sa

revision = "20261014_0051"
down_revision = "20261014_0050"
branch_labels = None
depends_on = None

def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table("red_team_results", schema="axiom"):
        return
    cols = {c["name"] for c in insp.get_columns("red_team_results", schema="axiom")}
    if "points_declared" not in cols:
        op.add_column("red_team_results", sa.Column("points_declared", sa.Integer(), nullable=False, server_default="0"), schema="axiom")
    if "points_awarded" not in cols:
        op.add_column("red_team_results", sa.Column("points_awarded", sa.Integer(), nullable=False, server_default="0"), schema="axiom")

def downgrade() -> None:
    pass
