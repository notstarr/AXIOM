"""Migrate legacy Pi Worker env bags to the canonical config shape."""

import json

from alembic import op
import sqlalchemy as sa


revision = "20260723_0016"
down_revision = "20260723_0015"
branch_labels = None
depends_on = None


LEGACY_FIELDS = {
    "PI_BINARY_PATH": "binary",
    "PI_MODEL": "model",
    "PI_BASE_URL": "base_url",
    "PI_PROVIDER_API": "provider_api",
    "PI_API_KEY": "api_key",
    "PI_THINKING": "thinking",
    "PI_MODEL_CONTEXT_WINDOW": "context_window",
}


def upgrade() -> None:
    connection = op.get_bind()
    rows = connection.execute(
        sa.text("SELECT name, worker_type, config_json FROM workers")
    ).mappings()
    for row in rows:
        if row["worker_type"] != "pi":
            continue
        try:
            source = json.loads(row["config_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            source = {}
        if not isinstance(source, dict):
            source = {}
        legacy_env = source.pop("env", {})
        if not isinstance(legacy_env, dict):
            legacy_env = {}
        environment = source.get("environment", {})
        if not isinstance(environment, dict):
            environment = {}
        for key, value in legacy_env.items():
            field = LEGACY_FIELDS.get(str(key))
            if field:
                if field not in source or source[field] in (None, ""):
                    source[field] = value
            else:
                environment[str(key)] = str(value)
        try:
            source["context_window"] = max(1, int(source.get("context_window", 200_000)))
        except (TypeError, ValueError):
            source["context_window"] = 200_000
        source["environment"] = {str(key): str(value) for key, value in environment.items()}
        connection.execute(
            sa.text("UPDATE workers SET config_json = :config WHERE name = :name"),
            {"name": row["name"], "config": json.dumps(source, ensure_ascii=False)},
        )


def downgrade() -> None:
    # The canonical shape is backward-compatible with the service and is not
    # safely reversible without deciding which arbitrary environment keys were
    # originally part of the legacy env bag.
    pass
