"""Normalize Goal direction in the graph and backfill terminal Reason edges."""

from alembic import op
import sqlalchemy as sa


revision = "20260724_0019"
down_revision = "20260723_0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()

    # Goal is a terminal Fact.  It must never remain in intent_sources.
    connection.execute(
        sa.text("DELETE FROM intent_sources WHERE fact_id = 'goal'")
    )

    # Older Reason completions recorded Goal Evaluation and completed the
    # Reason Intent, but did not persist the terminal edge.  Restore that edge
    # for the latest achieved evaluation of each task when it points to a
    # completed Reason Intent.
    connection.execute(
        sa.text(
            """
            UPDATE intents
            SET to_fact_id = 'goal'
            WHERE kind = 'reason'
              AND status = 'completed'
              AND to_fact_id IS NULL
              AND EXISTS (
                  SELECT 1
                  FROM goal_evaluations ge
                  WHERE ge.task_id = intents.task_id
                    AND ge.intent_id = intents.id
                    AND ge.achieved = 1
                    AND ge.created_at = (
                        SELECT MAX(latest.created_at)
                        FROM goal_evaluations latest
                        WHERE latest.task_id = ge.task_id
                          AND latest.achieved = 1
                    )
              )
            """
        )
    )


def downgrade() -> None:
    # The upgrade removes invalid historical source edges.  They cannot be
    # restored safely because their original ordering and intent ownership are
    # not meaningful graph semantics.
    pass
