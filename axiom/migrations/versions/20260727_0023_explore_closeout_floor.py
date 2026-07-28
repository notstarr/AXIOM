"""Raise unfinished Explore closeout budgets to a production-safe floor."""

from __future__ import annotations

from datetime import UTC, datetime
import json

from alembic import op
import sqlalchemy as sa


revision = "20260727_0023"
down_revision = "20260727_0022"
branch_labels = None
depends_on = None


MIN_EXPLORE_CONCLUDE_SECONDS = 180


def upgrade() -> None:
    connection = op.get_bind()
    _raise_runtime_floor(connection)
    connection.execute(
        sa.text(
            """
            UPDATE intents
            SET conclude_timeout_seconds = :minimum
            WHERE kind = 'explore'
              AND status IN ('pending', 'claimed', 'running', 'concluding')
              AND (
                conclude_timeout_seconds IS NULL
                OR conclude_timeout_seconds < :minimum
              )
            """
        ),
        {"minimum": MIN_EXPLORE_CONCLUDE_SECONDS},
    )
    connection.execute(
        sa.text(
            """
            UPDATE reason_runs
            SET explore_conclude_min_seconds = :minimum,
                explore_conclude_max_seconds = CASE
                  WHEN explore_conclude_max_seconds < :minimum THEN :minimum
                  ELSE explore_conclude_max_seconds
                END
            WHERE status IN ('pending', 'claimed', 'running', 'concluding')
              AND (
                explore_conclude_min_seconds < :minimum
                OR explore_conclude_max_seconds < :minimum
              )
            """
        ),
        {"minimum": MIN_EXPLORE_CONCLUDE_SECONDS},
    )


def downgrade() -> None:
    # This is a monotonic safety normalization. Keeping the larger budgets is
    # backward compatible with the previous schema and avoids corrupting values
    # that a user may have intentionally set to 180 seconds after the upgrade.
    pass


def _raise_runtime_floor(connection) -> None:
    rows = connection.execute(
        sa.text(
            """
            SELECT id, config_json, version
            FROM runtime_settings
            """
        )
    ).mappings()
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    for row in rows:
        try:
            payload = json.loads(row["config_json"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}

        minimum = _integer(payload.get("explore_conclude_min_seconds"), default=30)
        maximum = _integer(payload.get("explore_conclude_max_seconds"), default=600)
        target_minimum = max(minimum, MIN_EXPLORE_CONCLUDE_SECONDS)
        target_maximum = max(maximum, target_minimum)
        changed = target_minimum != minimum or target_maximum != maximum
        if not changed:
            continue
        payload["explore_conclude_min_seconds"] = target_minimum
        payload["explore_conclude_max_seconds"] = target_maximum

        connection.execute(
            sa.text(
                """
                UPDATE runtime_settings
                SET config_json = :config,
                    version = :version,
                    updated_by = :updated_by,
                    updated_at = :updated_at
                WHERE id = :id
                """
            ),
            {
                "id": row["id"],
                "config": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                "version": int(row["version"]) + 1,
                "updated_by": "migration.20260727_0023",
                "updated_at": now,
            },
        )


def _integer(value, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
