from __future__ import annotations

import json
import sys

from fastapi.testclient import TestClient

from axiom.infrastructure.database.engine import create_sqlite_engine
from axiom.infrastructure.database.metadata import metadata
from axiom.server.v2_app import create_v2_app


def test_mcp_imports_json_yaml_and_toml_and_binds_worker(tmp_path) -> None:
    engine = create_sqlite_engine(tmp_path / "axiom-v2.db")
    metadata.create_all(engine)
    app = create_v2_app(engine=engine, data_dir=tmp_path / "data")
    with TestClient(app) as client:
        assert client.post("/api/v1/mcp/import", json={"format": "json", "discover": False, "content": '{"mcpServers":{"browser":{"url":"http://127.0.0.1/sse"}}}'}).status_code == 201
        assert client.post("/api/v1/mcp/import", json={"format": "yaml", "discover": False, "content": "mcpServers:\n  shell:\n    command: sh\n"}).status_code == 201
        assert client.post("/api/v1/mcp/import", json={"format": "toml", "discover": False, "content": "[mcp_servers.docs]\ncommand='node'\nargs=['docs.js']\n"}).status_code == 201
        servers = client.get("/api/v1/mcp/servers").json()
        assert {item["name"] for item in servers} == {"browser", "shell", "docs"}

        worker = client.put("/api/v1/settings/workers/w1", json={"name": "w1", "worker_type": "pi", "capabilities": ["explore"], "max_running": 1, "config": {}, "disabled": False})
        assert worker.status_code == 200
        binding = client.put("/api/v1/mcp/workers/w1", json={"enabled": True, "server_ids": [servers[0]["id"]], "allowed_tools": ["browser:read"]})
        assert binding.status_code == 200
        assert binding.json()["enabled"] is True
        assert binding.json()["server_names"] == [servers[0]["name"]]


def test_mcp_import_discovers_tool_descriptions_schema_and_can_toggle(tmp_path) -> None:
    server_script = tmp_path / "fake_mcp.py"
    server_script.write_text(
        """
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("AXIOM discovery test")

@mcp.tool(title="Search documents")
def search(query: str, limit: int = 10) -> list[str]:
    \"\"\"Search indexed documents by keyword.\"\"\"
    return [query] * limit

if __name__ == "__main__":
    mcp.run(transport="stdio")
""".strip(),
        encoding="utf-8",
    )
    engine = create_sqlite_engine(tmp_path / "axiom-v2.db")
    metadata.create_all(engine)
    app = create_v2_app(engine=engine, data_dir=tmp_path / "data")
    config = {"mcpServers": {"fake": {"command": sys.executable, "args": [str(server_script)]}}}
    with TestClient(app) as client:
        response = client.post("/api/v1/mcp/import", json={"format": "json", "content": json.dumps(config)})
        assert response.status_code == 201, response.text
        server = response.json()[0]
        assert server["discovery_status"] == "ready"
        assert server["server_info"]["name"] == "AXIOM discovery test"
        assert len(server["tools"]) == 1
        tool = server["tools"][0]
        assert tool["name"] == "search"
        assert tool["title"] == "Search documents"
        assert tool["description"] == "Search indexed documents by keyword."
        assert tool["input_schema"]["properties"]["query"]["type"] == "string"
        assert tool["input_schema"]["properties"]["limit"]["default"] == 10
        assert tool["usage"] == "search(query: string, limit?: integer)"

        toggled = client.patch(
            f"/api/v1/mcp/servers/{server['id']}/tools/{tool['id']}", json={"enabled": False},
        )
        assert toggled.status_code == 200
        assert toggled.json()["enabled"] is False

        rediscovered = client.post(f"/api/v1/mcp/servers/{server['id']}/discover")
        assert rediscovered.status_code == 200
        assert rediscovered.json()["tools"][0]["enabled"] is False


def test_mcp_discovery_failure_keeps_server_and_reports_error(tmp_path) -> None:
    engine = create_sqlite_engine(tmp_path / "axiom-v2.db")
    metadata.create_all(engine)
    app = create_v2_app(engine=engine, data_dir=tmp_path / "data")
    content = json.dumps({"mcpServers": {"missing": {"command": "/definitely/not/an/mcp/server"}}})
    with TestClient(app) as client:
        response = client.post("/api/v1/mcp/import", json={"format": "json", "content": content})
        assert response.status_code == 201
        server = response.json()[0]
        assert server["discovery_status"] == "error"
        assert server["discovery_error"]
        listed = client.get("/api/v1/mcp/servers").json()
        assert [item["name"] for item in listed] == ["missing"]
