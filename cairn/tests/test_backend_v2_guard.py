from __future__ import annotations

from fastapi.testclient import TestClient

from axiom.guard.policy import GuardEvaluation, MODEL_SYSTEM_PROMPT
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

        bootstrap_cli_discovery = client.post(
            "/api/v1/internal/guard/pre",
            headers=headers,
            json={
                "task_id": "t_001",
                "phase": "bootstrap",
                "tool_name": "bash",
                "input": {"command": "which axiom-tool 2>&1; type axiom-tool 2>&1"},
            },
        ).json()
        assert bootstrap_cli_discovery["decision"] == "ALLOW"

        bootstrap_cli_help = client.post(
            "/api/v1/internal/guard/pre",
            headers=headers,
            json={
                "task_id": "t_001",
                "phase": "bootstrap",
                "tool_name": "bash",
                "input": {"command": "axiom-tool --help"},
            },
        ).json()
        assert bootstrap_cli_help["decision"] == "ALLOW"

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


def test_v2_guard_settings_persist_prompt_defaults_and_audit_details(tmp_path, monkeypatch) -> None:
    engine = create_sqlite_engine(tmp_path / "v2.db")
    metadata.create_all(engine)
    app = create_v2_app(engine=engine, service_token="token")
    headers = {"Authorization": "Bearer token"}
    captured = {}

    def fake_evaluate_guard_event(**kwargs):
        captured["system_prompt"] = kwargs["config"].system_prompt
        return GuardEvaluation(
            decision="ALLOW",
            reason="custom prompt used",
            source="model",
            normalized={"tool_name": "bash", "command": "curl https://example.com/private"},
            model_name="guard-model",
            model_latency_ms=17,
            raw_model_output="ALLOW",
        )

    monkeypatch.setattr("axiom.api.v1.guard.evaluate_guard_event", fake_evaluate_guard_event)

    with TestClient(app) as client:
        runtime = client.get("/api/v1/settings/runtime").json()
        assert runtime["config"]["guard_system_prompt"] == MODEL_SYSTEM_PROMPT
        assert runtime["config"]["guard_enabled_default"] is False

        defaults = client.get("/api/v1/guard/defaults")
        assert defaults.status_code == 200
        assert defaults.json()["system_prompt"] == MODEL_SYSTEM_PROMPT
        assert defaults.json()["hard_block_rules"]

        config = runtime["config"]
        config["guard_system_prompt"] = "CUSTOM GUARD PROMPT"
        config["guard_enabled_default"] = True
        config["guard_api_key"] = "test-key"
        saved = client.put("/api/v1/settings/runtime?actor=test", json=config)
        assert saved.status_code == 200
        assert saved.json()["config"]["guard_system_prompt"] == "CUSTOM GUARD PROMPT"
        assert saved.json()["config"]["guard_enabled_default"] is True

        decision = client.post(
            "/api/v1/internal/guard/pre",
            headers=headers,
            json={
                "project_id": "prj_001",
                "task_id": "t_001",
                "intent_id": "i002",
                "phase": "explore",
                "worker": "worker-a",
                "tool_name": "bash",
                "input": {"command": "curl https://example.com/private"},
            },
        )
        assert decision.status_code == 200
        assert captured["system_prompt"] == "CUSTOM GUARD PROMPT"

        events = client.get(
            "/api/v1/audit-events?event_type=guard.decision&task_id=t_001"
        ).json()
        assert events[0]["payload"]["command"] == "curl https://example.com/private"
        assert events[0]["payload"]["input"] == {
            "command": "curl https://example.com/private"
        }
        assert events[0]["payload"]["normalized"]["tool_name"] == "bash"
        assert events[0]["payload"]["model_name"] == "guard-model"
        assert events[0]["payload"]["model_latency_ms"] == 17
        assert events[0]["payload"]["raw_model_output"] == "ALLOW"
