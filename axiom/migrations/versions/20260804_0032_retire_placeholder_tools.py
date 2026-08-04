"""Retire the five placeholder AXIOM Tool packages from the catalog."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260804_0032"
down_revision = "20260803_0031"
branch_labels = None
depends_on = None


RETIRED_KEYS = (
    "find_files",
    "http_request",
    "list_directory",
    "run_python",
    "search_text",
)


def upgrade() -> None:
    connection = op.get_bind()
    parameters = {f"key_{index}": key for index, key in enumerate(RETIRED_KEYS)}
    placeholders = ",".join(f":key_{index}" for index in range(len(RETIRED_KEYS)))
    connection.execute(
        sa.text(
            "DELETE FROM phase_capability_bindings "
            "WHERE capability_kind = 'axiom_tool' "
            f"AND capability_key IN ({placeholders})"
        ),
        parameters,
    )
    connection.execute(
        sa.text(
            "DELETE FROM worker_axiom_tool_bindings "
            f"WHERE tool_key IN ({placeholders})"
        ),
        parameters,
    )
    connection.execute(
        sa.text(f"DELETE FROM axiom_tools WHERE key IN ({placeholders})"),
        parameters,
    )


def downgrade() -> None:
    # This is a data-only retirement.  Rolling the application code back
    # restores the bundled packages, and its startup scan recreates their
    # catalog rows without inventing historical bindings.
    pass
