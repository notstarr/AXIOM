"""Allow the existing Reviewer scheduler to review RedTeamResult subjects."""
from alembic import op
import sqlalchemy as sa

revision = "20261014_0050"
down_revision = "20261013_0049"
branch_labels = None
depends_on = None

def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table("vulnerability_review_runs", schema="axiom"):
        return
    cols = {c["name"] for c in insp.get_columns("vulnerability_review_runs", schema="axiom")}
    if "review_subject_type" not in cols:
        op.add_column("vulnerability_review_runs", sa.Column("review_subject_type", sa.String(), nullable=False, server_default="vulnerability"), schema="axiom")
    if "red_team_result_id" not in cols:
        op.add_column("vulnerability_review_runs", sa.Column("red_team_result_id", sa.String(), nullable=True), schema="axiom")
    if "vulnerability_id" in cols:
        op.alter_column("vulnerability_review_runs", "vulnerability_id", existing_type=sa.String(), nullable=True, schema="axiom")

def downgrade() -> None:
    pass
