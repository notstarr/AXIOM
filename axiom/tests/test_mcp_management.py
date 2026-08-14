from __future__ import annotations

import pytest

from axiom.modules.mcp.schema_order import PROPERTY_ORDER_KEY
from axiom.modules.mcp.service import parse_mcp_config
from axiom.modules.mcp.tables import mcp_tools


@pytest.mark.parametrize(
    ("format", "content"),
    [
        (
            "json",
            '{"mcpServers":{"demo":{"command":"uvx","args":["demo"],"env":{"TOKEN":"x"}}}}',
        ),
        (
            "yaml",
            "mcpServers:\n  demo:\n    command: uvx\n    args: [demo]\n    env:\n      TOKEN: x\n",
        ),
        (
            "toml",
            '[mcp_servers.demo]\ncommand = "uvx"\nargs = ["demo"]\n\n'
            '[mcp_servers.demo.env]\nTOKEN = "x"\n',
        ),
    ],
)
def test_parse_mcp_config_supports_common_formats(format: str, content: str) -> None:
    selected, servers = parse_mcp_config(content, format)

    assert selected == format
    assert len(servers) == 1
    assert servers[0].name == "demo"
    assert servers[0].command == "uvx"
    assert servers[0].args == ["demo"]
    assert servers[0].env == {"TOKEN": "x"}
    assert servers[0].transport == "stdio"


def test_mcp_import_exposes_tool_schema_usage_and_toggle(client) -> None:
    imported = client.post(
        "/api/v1/mcp/import",
        json={
            "format": "yaml",
            "discover": False,
            "content": "mcpServers:\n  demo:\n    command: uvx\n    args: [demo]\n",
        },
    )
    assert imported.status_code == 201, imported.text
    server = imported.json()[0]

    replaced = client.put(
        f"/api/v1/mcp/servers/{server['id']}/tools",
        json=[
            {
                "name": "search",
                "title": "站点搜索",
                "description": "搜索目标站点",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer"},
                    },
                    "required": ["query"],
                },
                "output_schema": {"type": "array"},
                "annotations": {"readOnlyHint": True},
            }
        ],
    )
    assert replaced.status_code == 200, replaced.text
    tool = replaced.json()[0]
    assert tool["title"] == "站点搜索"
    assert tool["description"] == "搜索目标站点"
    assert tool["input_schema"]["required"] == ["query"]
    assert tool["output_schema"] == {"type": "array"}
    assert tool["usage"] == "search(query: string, limit?: integer)"

    disabled = client.patch(
        f"/api/v1/mcp/servers/{server['id']}/tools/{tool['id']}",
        json={"enabled": False},
    )
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["enabled"] is False

    reenabled = client.put(
        f"/api/v1/mcp/servers/{server['id']}/tools/{tool['id']}",
        json={"enabled": True},
    )
    assert reenabled.status_code == 200, reenabled.text
    assert reenabled.json()["enabled"] is True


def test_mcp_jsonb_preserves_optional_parameter_signature_order(client) -> None:
    server = client.post(
        "/api/v1/mcp/import",
        json={
            "format": "json",
            "discover": False,
            "content": '{"mcpServers":{"demo":{"command":"demo"}}}',
        },
    ).json()[0]
    response = client.put(
        f"/api/v1/mcp/servers/{server['id']}/tools",
        json=[
            {
                "name": "ordered",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "level": {"type": "string"},
                        "task_id": {"type": "string"},
                        "title": {"type": "string"},
                        "page": {"type": "integer"},
                    },
                    "required": ["task_id"],
                },
            }
        ],
    )

    assert response.status_code == 200, response.text
    tool = response.json()[0]
    assert tool["usage"] == (
        "ordered(level?: string, task_id: string, title?: string, page?: integer)"
    )
    assert PROPERTY_ORDER_KEY not in tool["input_schema"]
    with client.app.state.container.engine.connect() as conn:
        raw = conn.execute(mcp_tools.select()).mappings().one()["input_schema_json"]
    assert PROPERTY_ORDER_KEY in raw


def test_mcp_worker_binding_is_read_only_compatibility_shape(client) -> None:
    client.put(
        "/api/v1/workers/worker-a",
        json={"capabilities": ["bootstrap", "reason", "explore"], "max_running": 2},
    )
    server = client.post(
        "/api/v1/mcp/import",
        json={
            "discover": False,
            "content": '{"mcpServers":{"demo":{"command":"uvx","args":["demo"]}}}',
        },
    ).json()[0]

    saved = client.put(
        "/api/v1/mcp/workers/worker-a",
        json={
            "enabled": True,
            "server_ids": [server["id"]],
            "allowed_tools": ["search"],
        },
    )
    assert saved.status_code == 409, saved.text

    worker = client.get("/api/v1/workers/worker-a").json()
    assert worker["config"].get("environment", {}).get("PI_MCP_ENABLED") != "true"

    bindings = client.get("/api/v1/mcp/workers")
    assert bindings.status_code == 200, bindings.text
    assert bindings.json()[0]["worker_name"] == "worker-a"
    assert bindings.json()[0]["enabled"] is False


def test_failed_discovery_preserves_tools_and_redacts_secrets(client, monkeypatch) -> None:
    server = client.post(
        "/api/v1/mcp/servers",
        json={
            "name": "remote",
            "url": "https://mcp.example.test/api?token=super-secret-token",
            "transport": "http",
            "disabled": True,
            "raw_config": {"headers": {"Authorization": "Bearer private-secret"}},
        },
    ).json()
    tool = client.put(
        f"/api/v1/mcp/servers/{server['id']}/tools",
        json=[{"name": "kept", "description": "must survive discovery failure"}],
    ).json()[0]

    async def fail(_config):
        raise RuntimeError(
            "request token=super-secret-token Authorization=Bearer private-secret"
        )

    monkeypatch.setattr("axiom.modules.mcp.service.discover_mcp_server", fail)
    discovered = client.post(f"/api/v1/mcp/servers/{server['id']}/discover")

    assert discovered.status_code == 200, discovered.text
    body = discovered.json()
    assert body["discovery_status"] == "error"
    assert [item["id"] for item in body["tools"]] == [tool["id"]]
    assert "super-secret-token" not in body["discovery_error"]
    assert "private-secret" not in body["discovery_error"]
    assert "***" in body["discovery_error"]


def test_jsonb_key_order_does_not_force_mcp_rediscovery(client) -> None:
    created = client.post(
        "/api/v1/mcp/servers",
        json={
            "name": "jsonb-order",
            "command": "demo",
            "args": ["--serve"],
            "env": {"SECOND": "2", "FIRST": "1"},
            "raw_config": {"headers": {"X-Second": "2", "X-First": "1"}},
        },
    ).json()
    client.put(
        f"/api/v1/mcp/servers/{created['id']}/tools",
        json=[{"name": "ready"}],
    )

    updated = client.put(
        f"/api/v1/mcp/servers/{created['id']}",
        json={
            "name": "jsonb-order",
            "command": "demo",
            "args": ["--serve"],
            "env": {"FIRST": "1", "SECOND": "2"},
            "raw_config": {"headers": {"X-First": "1", "X-Second": "2"}},
        },
    )

    assert updated.status_code == 200, updated.text
    assert updated.json()["discovery_status"] == "ready"
