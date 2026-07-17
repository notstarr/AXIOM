from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from axiom.dispatcher.config import DispatchConfig, WorkerConfig, validate_prompt_resources
from axiom.dispatcher.tools.tracker import _is_tool_error
from axiom.dispatcher.workers.adapters.pi import PiDriver
from axiom.server import db

from conftest import make_config


def test_dispatch_config_merges_common_env_with_worker_override() -> None:
    payload = make_config().model_dump()
    payload["common_env"] = {
        "SHARED": "common",
        "OVERRIDE": "common",
        "PI_MODEL": "model-from-common",
        "PI_BASE_URL": "http://common-api",
        "PI_API_KEY": "common-key",
        "PI_PROVIDER_API": "responses",
    }
    payload["workers"][0]["env"] = {"OVERRIDE": "worker"}

    config = DispatchConfig.model_validate(payload)

    assert config.workers[0].env["SHARED"] == "common"
    assert config.workers[0].env["OVERRIDE"] == "worker"


def test_mcp_tool_error_detection_handles_adapter_error_text() -> None:
    assert _is_tool_error(
        tool_name="mcp",
        output="Failed to call tool: MCP error -32001: Request timed out",
        is_error=False,
        details={"error": "call_failed"},
    )
    assert _is_tool_error(
        tool_name="mcp",
        output='{"error":"wx is not defined"}',
        is_error=False,
    )
    assert not _is_tool_error(
        tool_name="mcp",
        output='{"ok":true}',
        is_error=False,
    )


def test_dispatch_config_defaults_worker_healthcheck_and_rejects_unknown_mode() -> None:
    payload = make_config().model_dump()
    payload["runtime"].pop("worker_healthcheck")

    assert DispatchConfig.model_validate(payload).runtime.worker_healthcheck == "startup_only"

    payload["runtime"]["worker_healthcheck"] = "sometimes"
    with pytest.raises(ValidationError):
        DispatchConfig.model_validate(payload)


def test_dispatch_config_rejects_duplicate_workers_and_excess_project_parallelism() -> None:
    payload = make_config().model_dump()
    payload["workers"].append(dict(payload["workers"][0]))
    with pytest.raises(ValidationError, match="worker names must be unique"):
        DispatchConfig.model_validate(payload)

    payload = make_config().model_dump()
    payload["runtime"]["max_project_workers"] = 3
    with pytest.raises(ValidationError, match="max_project_workers cannot exceed max_workers"):
        DispatchConfig.model_validate(payload)


def test_dispatch_config_ignores_extra_container_field() -> None:
    """container 字段在本地模式下被静默忽略"""
    payload = make_config().model_dump()
    payload["container"] = {
        "image": "old-image",
        "network_mode": "host",
        "completed_action": "stop",
    }
    config = DispatchConfig.model_validate(payload)
    assert config is not None
    # container 字段不在 DispatchConfig 的 model_fields 中（被 extra="ignore" 忽略）
    assert "container" not in DispatchConfig.model_fields


def test_pi_worker_rejects_invalid_context_window() -> None:
    with pytest.raises(ValidationError, match="PI_MODEL_CONTEXT_WINDOW must be greater than 0"):
        WorkerConfig.model_validate(
            {
                "name": "pi",
                "type": "pi",
                "task_types": ["explore"],
                "max_running": 1,
                "priority": 0,
                "env": {
                    "PI_MODEL": "model",
                    "PI_BASE_URL": "http://api",
                    "PI_API_KEY": "secret",
                    "PI_PROVIDER_API": "openai-completions",
                    "PI_MODEL_CONTEXT_WINDOW": "0",
                },
            }
        )


def test_pi_worker_rejects_invalid_thinking_level() -> None:
    with pytest.raises(ValidationError, match="PI_THINKING must be one of"):
        WorkerConfig.model_validate(
            {
                "name": "pi",
                "type": "pi",
                "task_types": ["explore"],
                "max_running": 1,
                "priority": 0,
                "env": {
                    "PI_MODEL": "model",
                    "PI_BASE_URL": "http://api",
                    "PI_API_KEY": "secret",
                    "PI_PROVIDER_API": "openai-completions",
                    "PI_THINKING": "turbo",
                },
            }
        )


def test_worker_only_accepts_pi_type() -> None:
    """仅支持 pi 类型，其他类型被拒绝"""
    with pytest.raises(ValidationError, match="Input should be 'pi'"):
        WorkerConfig.model_validate(
            {
                "name": "other",
                "type": "other",
                "task_types": ["explore"],
                "max_running": 1,
                "priority": 0,
                "env": {},
            }
        )


def test_bundled_default_prompt_group_has_required_placeholders() -> None:
    validate_prompt_resources("default")


def test_pi_driver_models_json_and_execute_argv_include_context_window_and_tools() -> None:
    worker = WorkerConfig.model_validate(
        {
            "name": "pi-worker",
            "type": "pi",
            "task_types": ["explore"],
            "max_running": 1,
            "priority": 0,
            "env": {
                "PI_MODEL": "model",
                "PI_BASE_URL": "http://api",
                "PI_API_KEY": "secret",
                "PI_PROVIDER_API": "openai-completions",
                "PI_MODEL_CONTEXT_WINDOW": "131072",
            },
        }
    )

    result = PiDriver().build_execute(worker, "prompt", None)
    models = json.loads(result.argv[5])

    assert models["providers"]["axiom"]["models"][0]["contextWindow"] == 131072
    assert "--tools" in result.argv
    tools_value = result.argv[result.argv.index("--tools") + 1]
    assert "mcp" not in tools_value.split(",")
    assert "--no-extensions" in result.argv
    assert "--no-skills" in result.argv
    assert result.argv[-2:] == ["-p", "prompt"]


def test_pi_driver_execute_argv_includes_worker_thinking_level() -> None:
    worker = WorkerConfig.model_validate(
        {
            "name": "pi-worker-thinking",
            "type": "pi",
            "task_types": ["explore"],
            "max_running": 1,
            "priority": 0,
            "env": {
                "PI_MODEL": "model",
                "PI_BASE_URL": "http://api",
                "PI_API_KEY": "secret",
                "PI_PROVIDER_API": "openai-completions",
                "PI_THINKING": "high",
            },
        }
    )

    result = PiDriver().build_execute(worker, "prompt", None)

    assert "--thinking" in result.argv
    assert result.argv[result.argv.index("--thinking") + 1] == "high"
    assert result.argv[-2:] == ["-p", "prompt"]


def test_pi_driver_execute_argv_omits_off_thinking_level() -> None:
    worker = WorkerConfig.model_validate(
        {
            "name": "pi-worker-thinking-off",
            "type": "pi",
            "task_types": ["explore"],
            "max_running": 1,
            "priority": 0,
            "env": {
                "PI_MODEL": "model",
                "PI_BASE_URL": "http://api",
                "PI_API_KEY": "secret",
                "PI_PROVIDER_API": "openai-completions",
                "PI_THINKING": "off",
            },
        }
    )

    result = PiDriver().build_execute(worker, "prompt", None)

    assert "--thinking" not in result.argv


def test_pi_driver_enables_mcp_per_worker_and_writes_mcp_config() -> None:
    worker = WorkerConfig.model_validate(
        {
            "name": "pi-worker-mcp",
            "type": "pi",
            "task_types": ["explore"],
            "max_running": 1,
            "priority": 0,
            "env": {
                "PI_MODEL": "model",
                "PI_BASE_URL": "http://api",
                "PI_API_KEY": "secret",
                "PI_PROVIDER_API": "openai-completions",
                "PI_MCP_ENABLED": "true",
                "PI_MCP_CONFIG_JSON": json.dumps(
                    {
                        "mcpServers": {
                            "github": {
                                "command": "npx",
                                "args": ["-y", "@modelcontextprotocol/server-github"],
                            }
                        }
                    }
                ),
            },
        }
    )
    driver = PiDriver()
    result = driver.build_execute(worker, "prompt", None)

    assert "--tools" in result.argv
    tools_value = result.argv[result.argv.index("--tools") + 1]
    assert "mcp" in tools_value.split(",")
    assert "--no-extensions" not in result.argv
    mcp_config_path = Path(driver._agent_dir(worker)) / "mcp.json"
    assert "--mcp-config" in result.argv
    assert result.argv[result.argv.index("--mcp-config") + 1] == str(mcp_config_path)
    assert mcp_config_path.exists()
    payload = json.loads(mcp_config_path.read_text(encoding="utf-8"))
    assert "github" in payload.get("mcpServers", {})


def test_pi_driver_removes_stale_mcp_config_when_disabled() -> None:
    worker = WorkerConfig.model_validate(
        {
            "name": "pi-worker-mcp-disabled",
            "type": "pi",
            "task_types": ["explore"],
            "max_running": 1,
            "priority": 0,
            "env": {
                "PI_MODEL": "model",
                "PI_BASE_URL": "http://api",
                "PI_API_KEY": "secret",
                "PI_PROVIDER_API": "openai-completions",
                "PI_MCP_ENABLED": "false",
            },
        }
    )
    driver = PiDriver()
    mcp_config_path = Path(driver._agent_dir(worker)) / "mcp.json"
    mcp_config_path.parent.mkdir(parents=True, exist_ok=True)
    mcp_config_path.write_text('{"mcpServers":{"first-miniapp":{}}}', encoding="utf-8")

    result = driver.build_execute(worker, "prompt", None)

    assert "--no-extensions" in result.argv
    assert "--mcp-config" not in result.argv
    assert not mcp_config_path.exists()


def test_pi_driver_mcp_allowlist_filters_servers_and_direct_tools() -> None:
    worker = WorkerConfig.model_validate(
        {
            "name": "pi-worker-mcp-allow",
            "type": "pi",
            "task_types": ["explore"],
            "max_running": 1,
            "priority": 0,
            "env": {
                "PI_MODEL": "model",
                "PI_BASE_URL": "http://api",
                "PI_API_KEY": "secret",
                "PI_PROVIDER_API": "openai-completions",
                "PI_MCP_ENABLED": "1",
                "PI_MCP_ALLOWED_SERVERS": "github",
                "PI_MCP_ALLOWED_TOOLS": "search_repositories,get_file_contents",
                "PI_MCP_CONFIG_JSON": json.dumps(
                    {
                        "mcpServers": {
                            "github": {
                                "command": "npx",
                                "args": ["-y", "@modelcontextprotocol/server-github"],
                                "directTools": ["search_repositories", "delete_repository"],
                            },
                            "filesystem": {
                                "command": "npx",
                                "args": ["-y", "@modelcontextprotocol/server-filesystem"],
                            },
                        }
                    }
                ),
            },
        }
    )
    driver = PiDriver()
    driver.build_execute(worker, "prompt", None)
    mcp_config_path = Path(driver._agent_dir(worker)) / "mcp.json"
    payload = json.loads(mcp_config_path.read_text(encoding="utf-8"))
    assert sorted(payload["mcpServers"].keys()) == ["github"]
    assert payload["mcpServers"]["github"]["directTools"] == ["search_repositories"]


def test_pi_driver_applies_global_mcp_tool_registry_and_worker_scoped_allowlist(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "axiom.db")
    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO mcp_servers (id, name, command, args_json, env_json, disabled, url, created_at, updated_at)
            VALUES ('mcp_one', 'first-miniapp', '', '[]', '{}', 0, 'http://127.0.0.1:4554/sse', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
            """
        )
        conn.execute(
            """
            INSERT INTO mcp_server_tools (id, server_id, server_name, name, description, enabled, source, created_at, updated_at)
            VALUES
              ('tool_read', 'mcp_one', 'first-miniapp', 'read_page', '', 1, 'manual', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'),
              ('tool_click', 'mcp_one', 'first-miniapp', 'click', '', 1, 'manual', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'),
              ('tool_type', 'mcp_one', 'first-miniapp', 'type_text', '', 0, 'manual', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
            """
        )
    worker = WorkerConfig.model_validate(
        {
            "name": "pi-worker-mcp-global-tools",
            "type": "pi",
            "task_types": ["explore"],
            "max_running": 1,
            "priority": 0,
            "env": {
                "PI_MODEL": "model",
                "PI_BASE_URL": "http://api",
                "PI_API_KEY": "secret",
                "PI_PROVIDER_API": "openai-completions",
                "PI_MCP_ENABLED": "1",
                "PI_MCP_ALLOWED_SERVERS": "first-miniapp",
                "PI_MCP_ALLOWED_TOOLS": "first-miniapp:read_page",
                "PI_MCP_CONFIG_JSON": "",
            },
        }
    )
    driver = PiDriver()
    driver.build_execute(worker, "prompt", None)
    mcp_config_path = Path(driver._agent_dir(worker)) / "mcp.json"
    payload = json.loads(mcp_config_path.read_text(encoding="utf-8"))
    server = payload["mcpServers"]["first-miniapp"]
    assert server["url"] == "http://127.0.0.1:4554/sse"
    assert server["directTools"] == ["read_page"]


def test_pi_driver_prepares_stable_session_for_execute_and_conclude() -> None:
    worker = WorkerConfig.model_validate(
        {
            "name": "pi-worker",
            "type": "pi",
            "task_types": ["explore"],
            "max_running": 1,
            "priority": 0,
            "env": {
                "PI_MODEL": "model",
                "PI_BASE_URL": "http://api",
                "PI_API_KEY": "secret",
                "PI_PROVIDER_API": "openai-completions",
            },
        }
    )
    driver = PiDriver()
    driver._session_id_supported = {driver._resolve_pi_path(worker): True}
    session = "session-preallocated-001"
    execute = driver.build_execute(worker, "execute-prompt", session)
    assert "--session-id" in execute.argv
    assert execute.argv[execute.argv.index("--session-id") + 1] == session
    assert execute.session == session

    sessions_dir = Path(driver._session_dir(worker))
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / f"2026-07-01T00-00-00_{session}.jsonl").write_text("{}", encoding="utf-8")
    execute_resume = driver.build_execute(worker, "execute-prompt", session)
    assert "--session-id" in execute_resume.argv
    assert execute_resume.argv[execute_resume.argv.index("--session-id") + 1] == session

    conclude = driver.build_conclude(worker, "conclude-prompt", session)
    assert "--session-id" in conclude
    assert conclude[conclude.index("--session-id") + 1] == session


def test_pi_driver_extract_session_uses_stdout_first() -> None:
    driver = PiDriver()
    stdout = '{"type":"session","id":"pi-session-real"}\n{"type":"done"}'
    resolved = driver.extract_session("preallocated-session", stdout, "")
    assert resolved == "pi-session-real"


def test_pi_driver_extract_session_invalid_preallocated_returns_none(monkeypatch) -> None:
    driver = PiDriver()
    monkeypatch.setattr(driver, "_session_id_exists_globally", lambda _session_id: False)
    resolved = driver.extract_session("preallocated-session", "", "")
    assert resolved is None


def test_pi_worker_configuration_is_accepted() -> None:
    worker = WorkerConfig.model_validate(
        {
            "name": "pi-alpha",
            "type": "pi",
            "task_types": ["reason"],
            "max_running": 1,
            "priority": 0,
            "env": {
                "PI_MODEL": "gpt-test",
                "PI_BASE_URL": "http://api/v1",
                "PI_API_KEY": "secret",
                "PI_PROVIDER_API": "responses",
            },
        }
    )

    assert worker.type == "pi"
