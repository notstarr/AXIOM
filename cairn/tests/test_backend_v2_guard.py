from __future__ import annotations

from fastapi.testclient import TestClient

from axiom.infrastructure.database.engine import create_sqlite_engine
from axiom.infrastructure.database.metadata import metadata
from axiom.server.v2_app import create_v2_app


def test_v2_guard_uses_phase_permissions_hard_rules_and_fail_open(tmp_path) -> None:
    engine = create_sqlite_engine(tmp_path / "v2.db")
    metadata.create_all(engine)
    app = create_v2_app(engine=engine, service_token="token")
    headers = {"Authorization": "Bearer token"}
    with TestClient(app) as client:
        allowed_cli = client.post(
            "/api/v1/internal/guard/pre",
            headers=headers,
            json={
                "task_id": "t_001",
                "phase": "reason",
                "tool_name": "bash",
                "input": {"command": "axiom-tool todo list"},
            },
        ).json()
        assert allowed_cli["decision"] == "ALLOW"

        blocked_cli = client.post(
            "/api/v1/internal/guard/pre",
            headers=headers,
            json={
                "task_id": "t_001",
                "phase": "explore",
                "tool_name": "bash",
                "input": {"command": "axiom-tool intent propose --help"},
            },
        ).json()
        assert blocked_cli["decision"] == "BLOCK"

        hard_blocked = client.post(
            "/api/v1/internal/guard/pre",
            headers=headers,
            json={
                "task_id": "t_001",
                "phase": "explore",
                "tool_name": "bash",
                "input": {"command": "curl https://example.com && systemctl restart nginx"},
            },
        ).json()
        assert hard_blocked["decision"] == "BLOCK"
        assert hard_blocked["source"] == "hard_rule"

        fail_open = client.post(
            "/api/v1/internal/guard/pre",
            headers=headers,
            json={
                "task_id": "t_001",
                "phase": "explore",
                "tool_name": "bash",
                "input": {"command": "curl https://example.com/api/users/1"},
            },
        ).json()
        assert fail_open["decision"] == "ALLOW"
        assert fail_open["source"] == "fail_open"
