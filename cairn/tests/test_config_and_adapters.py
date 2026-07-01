from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from axiom.dispatcher.config import DispatchConfig, WorkerConfig, validate_prompt_resources
from axiom.dispatcher.workers.adapters.pi import PiDriver

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
    assert result.argv[-2:] == ["-p", "prompt"]


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
    session = "session-preallocated-001"
    execute = driver.build_execute(worker, "execute-prompt", session)
    assert "--session" not in execute.argv
    assert execute.session == session

    sessions_dir = Path(driver._session_dir(worker))
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / f"2026-07-01T00-00-00_{session}.jsonl").write_text("{}", encoding="utf-8")
    execute_resume = driver.build_execute(worker, "execute-prompt", session)
    assert "--session" in execute_resume.argv
    assert execute_resume.argv[execute_resume.argv.index("--session") + 1] == session

    conclude = driver.build_conclude(worker, "conclude-prompt", session)
    assert "--session" in conclude
    assert conclude[conclude.index("--session") + 1] == session


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
