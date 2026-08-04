"""Replace model-Worker bindings with logical stage capability scopes."""

from __future__ import annotations

from datetime import datetime, timezone
import json

from alembic import op
import sqlalchemy as sa


revision = "20260803_0031"
down_revision = "20260803_0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "phase_capability_bindings",
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("capability_kind", sa.String(), nullable=False),
        sa.Column("capability_key", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.CheckConstraint(
            "scope IN ('common','bootstrap','reason','explore')",
            name="ck_phase_capability_bindings_scope",
        ),
        sa.CheckConstraint(
            "capability_kind IN ('skill','mcp_tool','axiom_tool')",
            name="ck_phase_capability_bindings_capability_kind",
        ),
        sa.UniqueConstraint(
            "scope",
            "capability_kind",
            "capability_key",
            name="uq_phase_capability_bindings_scope_kind_key",
        ),
    )
    op.create_index(
        "ix_phase_capability_bindings_kind_key",
        "phase_capability_bindings",
        ["capability_kind", "capability_key"],
    )

    connection = op.get_bind()
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    rows: set[tuple[str, str, str]] = {
        ("common", "skill", "whatisvul")
    }

    for row in connection.execute(
        sa.text(
            "SELECT phase, tool_key FROM worker_axiom_tool_bindings "
            "WHERE phase IN ('bootstrap','reason','explore')"
        )
    ).mappings():
        rows.add((str(row["phase"]), "axiom_tool", str(row["tool_key"])))

    tools = list(
        connection.execute(
            sa.text(
                "SELECT t.id, t.server_id, t.name, t.enabled, s.name AS server_name, "
                "s.disabled AS server_disabled "
                "FROM mcp_tools t JOIN mcp_servers s ON s.id = t.server_id"
            )
        ).mappings()
    )
    tools_by_server: dict[str, list[dict]] = {}
    for tool in tools:
        tools_by_server.setdefault(str(tool["server_id"]), []).append(dict(tool))

    for worker in connection.execute(
        sa.text("SELECT capabilities_json, config_json FROM workers")
    ).mappings():
        try:
            capabilities = json.loads(worker["capabilities_json"] or "[]")
            config = json.loads(worker["config_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(capabilities, list) or not isinstance(config, dict):
            continue
        binding = config.get("mcp") if isinstance(config.get("mcp"), dict) else None
        if binding is None:
            environment = (
                config.get("environment")
                if isinstance(config.get("environment"), dict)
                else {}
            )
            enabled = str(environment.get("PI_MCP_ENABLED") or "").lower() == "true"
            if not enabled:
                continue
            allowed = [
                item.strip()
                for item in str(environment.get("PI_MCP_ALLOWED_TOOLS") or "").split(",")
                if item.strip()
            ]
            try:
                runtime = json.loads(environment.get("PI_MCP_CONFIG_JSON") or "{}")
            except (TypeError, json.JSONDecodeError):
                runtime = {}
            selected_server_names = set(
                (runtime.get("mcpServers") or {}).keys()
                if isinstance(runtime, dict) and isinstance(runtime.get("mcpServers"), dict)
                else []
            )
            server_ids = {
                str(tool["server_id"])
                for tool in tools
                if str(tool["server_name"]) in selected_server_names
            }
        else:
            if not binding.get("enabled"):
                continue
            server_ids = {str(item) for item in binding.get("server_ids") or []}
            allowed = [str(item).strip() for item in binding.get("allowed_tools") or []]

        selected: list[dict] = []
        for server_id in server_ids:
            selected.extend(tools_by_server.get(server_id, []))
        selected = [
            tool
            for tool in selected
            if bool(tool["enabled"]) and not bool(tool["server_disabled"])
        ]
        if allowed:
            allowed_set = set(allowed)
            selected = [
                tool
                for tool in selected
                if str(tool["id"]) in allowed_set
                or str(tool["name"]) in allowed_set
                or f"{tool['server_name']}:{tool['name']}" in allowed_set
            ]
        for phase in capabilities:
            if phase not in {"bootstrap", "reason", "explore"}:
                continue
            for tool in selected:
                rows.add((str(phase), "mcp_tool", str(tool["id"])))

    if rows:
        connection.execute(
            sa.text(
                "INSERT OR IGNORE INTO phase_capability_bindings "
                "(scope, capability_kind, capability_key, created_at, updated_at) "
                "VALUES (:scope, :kind, :key, :created_at, :updated_at)"
            ),
            [
                {
                    "scope": scope,
                    "kind": kind,
                    "key": key,
                    "created_at": now,
                    "updated_at": now,
                }
                for scope, kind, key in sorted(rows)
            ],
        )


def downgrade() -> None:
    op.drop_index(
        "ix_phase_capability_bindings_kind_key",
        table_name="phase_capability_bindings",
    )
    op.drop_table("phase_capability_bindings")
