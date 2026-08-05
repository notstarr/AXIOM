"""PostgreSQL native baseline.

Revision ID: 20260805_pg0001
Revises: none
Create Date: 2026-08-05
"""

from __future__ import annotations

from alembic import op

from axiom.shared import model_registry as _model_registry  # noqa: F401
from axiom.shared.database import AXIOM_SCHEMA, metadata


revision = "20260805_pg0001"
down_revision = None
branch_labels = ("postgresql",)
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    metadata.create_all(bind=bind, checkfirst=False)
    op.execute(f"REVOKE CREATE ON SCHEMA {AXIOM_SCHEMA} FROM PUBLIC")
    op.execute(f"GRANT USAGE ON SCHEMA {AXIOM_SCHEMA} TO axiom_app")
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {AXIOM_SCHEMA} "
        "TO axiom_app"
    )
    op.execute(
        f"GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA {AXIOM_SCHEMA} "
        "TO axiom_app"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES FOR ROLE axiom_owner IN SCHEMA {AXIOM_SCHEMA} "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO axiom_app"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES FOR ROLE axiom_owner IN SCHEMA {AXIOM_SCHEMA} "
        "GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO axiom_app"
    )


def downgrade() -> None:
    metadata.drop_all(bind=op.get_bind(), checkfirst=True)
