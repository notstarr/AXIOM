"""Add isolated red-team chains and results."""
from alembic import op
import sqlalchemy as sa

revision = "20261013_0049"
down_revision = "20261012_0048"
branch_labels = None
depends_on = None

SCHEMA = "axiom"


def _has(bind, name):
    return sa.inspect(bind).has_table(name, schema=SCHEMA)


def upgrade() -> None:
    bind = op.get_bind()
    if _has(bind, "tasks"):
        cols = {c["name"] for c in sa.inspect(bind).get_columns("tasks", schema=SCHEMA)}
        if "engagement_mode" not in cols:
            op.add_column("tasks", sa.Column("engagement_mode", sa.String(), nullable=False, server_default="vulnerability"), schema=SCHEMA)
        if "red_team_objective_json" not in cols:
            op.add_column("tasks", sa.Column("red_team_objective_json", sa.Text(), nullable=True), schema=SCHEMA)
    if not _has(bind, "red_team_chains"):
        op.create_table(
            "red_team_chains",
            sa.Column("id", sa.String(), primary_key=True), sa.Column("task_id", sa.String(), nullable=False),
            sa.Column("goal_cycle_id", sa.String()), sa.Column("title", sa.Text(), nullable=False),
            sa.Column("initial_principal_json", sa.Text(), nullable=False), sa.Column("target_capabilities_json", sa.Text(), nullable=False),
            sa.Column("target_assets_json", sa.Text(), nullable=False), sa.Column("status", sa.String(), nullable=False),
            sa.Column("current_state_json", sa.Text(), nullable=False), sa.Column("source_fact_ids_json", sa.Text(), nullable=False),
            sa.Column("source_intent_ids_json", sa.Text(), nullable=False), sa.Column("created_by", sa.String(), nullable=False),
            sa.Column("created_at", sa.Text(), nullable=False), sa.Column("updated_at", sa.Text(), nullable=False), schema=SCHEMA,
        )
    if not _has(bind, "red_team_results"):
        op.create_table(
            "red_team_results", sa.Column("id", sa.String(), primary_key=True), sa.Column("task_id", sa.String(), nullable=False),
            sa.Column("chain_id", sa.String(), nullable=False), sa.Column("intent_id", sa.String(), nullable=False),
            sa.Column("result_type", sa.String(), nullable=False), sa.Column("title", sa.Text(), nullable=False), sa.Column("description", sa.Text(), nullable=False),
            sa.Column("principal", sa.String(), nullable=False), sa.Column("tenant", sa.String(), nullable=False),
            sa.Column("capabilities_before_json", sa.Text(), nullable=False), sa.Column("capabilities_gained_json", sa.Text(), nullable=False), sa.Column("capabilities_after_json", sa.Text(), nullable=False),
            sa.Column("target_asset", sa.String(), nullable=False), sa.Column("data_target_id", sa.String()), sa.Column("sample_count", sa.Integer()), sa.Column("sample_bytes", sa.Integer()),
            sa.Column("data_fields_json", sa.Text(), nullable=False), sa.Column("data_summary", sa.Text(), nullable=False), sa.Column("credential_ref", sa.String()), sa.Column("evidence_refs_json", sa.Text(), nullable=False),
            sa.Column("status", sa.String(), nullable=False), sa.Column("reported_by", sa.String(), nullable=False), sa.Column("reviewed_by", sa.String()), sa.Column("review_reason", sa.Text(), nullable=False),
            sa.Column("created_at", sa.Text(), nullable=False), sa.Column("updated_at", sa.Text(), nullable=False), schema=SCHEMA,
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _has(bind, "red_team_results"):
        op.drop_table("red_team_results", schema=SCHEMA)
    if _has(bind, "red_team_chains"):
        op.drop_table("red_team_chains", schema=SCHEMA)
