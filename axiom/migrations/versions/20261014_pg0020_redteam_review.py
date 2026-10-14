"""Allow the existing Reviewer scheduler to review RedTeamResult subjects."""
from alembic import op
import sqlalchemy as sa
from axiom.shared.database import AXIOM_SCHEMA

revision = "20261014_pg0020"
down_revision = "20261013_pg0019"
branch_labels = None
depends_on = None

def upgrade() -> None:
    bind = op.get_bind(); insp = sa.inspect(bind)
    if not insp.has_table("vulnerability_review_runs", schema=AXIOM_SCHEMA): return
    cols = {c["name"] for c in insp.get_columns("vulnerability_review_runs", schema=AXIOM_SCHEMA)}
    if "review_subject_type" not in cols:
        op.add_column("vulnerability_review_runs", sa.Column("review_subject_type", sa.String(), nullable=False, server_default="vulnerability"), schema=AXIOM_SCHEMA)
    if "red_team_result_id" not in cols:
        op.add_column("vulnerability_review_runs", sa.Column("red_team_result_id", sa.String(), nullable=True), schema=AXIOM_SCHEMA)
    op.alter_column("vulnerability_review_runs", "vulnerability_id", existing_type=sa.String(), nullable=True, schema=AXIOM_SCHEMA)

def downgrade() -> None:
    pass
