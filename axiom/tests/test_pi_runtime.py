from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from axiom.dispatcher.runner import AxiomRunner
from axiom.modules.workers import pi_config
from axiom.modules.workers.schemas import Worker


def _version_script(path: Path, version: str) -> Path:
    path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{version}'\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_canonicalize_pi_worker_config_replaces_legacy_binary(monkeypatch, tmp_path) -> None:
    runtime = tmp_path / "pi"
    monkeypatch.setattr(pi_config, "PI_RUNTIME_BINARY", runtime)

    normalized = pi_config.canonicalize_pi_worker_config(
        {"binary": "/usr/local/bin/pi", "model": "test-model"}
    )

    assert normalized["binary"] == str(runtime)


def test_canonicalize_pi_worker_config_splits_legacy_thinking_setting() -> None:
    enabled = pi_config.canonicalize_pi_worker_config(
        {"thinking": "HIGH", "thinking_capable": "true", "thinking_tested_at": "old"}
    )
    assert enabled["thinking_enabled"] is True
    assert enabled["thinking_level"] == "high"
    assert "thinking" not in enabled
    assert "thinking_capable" not in enabled
    assert "thinking_tested_at" not in enabled

    disabled = pi_config.canonicalize_pi_worker_config({"thinking": "off"})
    assert disabled["thinking_enabled"] is False
    assert disabled["thinking_level"] == "medium"


def test_pi_thinking_level_rejects_values_not_supported_by_pi() -> None:
    with pytest.raises(ValueError):
        pi_config.PiWorkerConfig.model_validate(
            {"thinking_enabled": True, "thinking_level": "ultra"}
        )


def test_validate_pi_worker_runtime_checks_pinned_pi_and_node(monkeypatch, tmp_path) -> None:
    runtime = _version_script(tmp_path / "pi", pi_config.PI_RUNTIME_VERSION)
    node = _version_script(tmp_path / "node", pi_config.PI_NODE_VERSION)
    monkeypatch.setattr(pi_config, "PI_RUNTIME_BINARY", runtime)
    monkeypatch.setattr(pi_config, "PI_NODE_BINARY", node)
    pi_config._validate_pi_runtime.cache_clear()

    settings = pi_config.validate_pi_worker_runtime(
        {"binary": "pi", "model": "test-model"}
    )

    assert settings.binary == str(runtime)


def test_validate_pi_worker_runtime_rejects_unpinned_binary(monkeypatch, tmp_path) -> None:
    runtime = _version_script(tmp_path / "pi", pi_config.PI_RUNTIME_VERSION)
    node = _version_script(tmp_path / "node", pi_config.PI_NODE_VERSION)
    other = _version_script(tmp_path / "other-pi", pi_config.PI_RUNTIME_VERSION)
    monkeypatch.setattr(pi_config, "PI_RUNTIME_BINARY", runtime)
    monkeypatch.setattr(pi_config, "PI_NODE_BINARY", node)
    pi_config._validate_pi_runtime.cache_clear()

    with pytest.raises(pi_config.PiRuntimeError, match="必须使用版本化运行时"):
        pi_config.validate_pi_worker_runtime(
            {"binary": str(other), "model": "test-model"}
        )


def test_validate_pi_worker_runtime_rejects_wrong_node_version(monkeypatch, tmp_path) -> None:
    runtime = _version_script(tmp_path / "pi", pi_config.PI_RUNTIME_VERSION)
    node = _version_script(tmp_path / "node", "v22.15.0")
    monkeypatch.setattr(pi_config, "PI_RUNTIME_BINARY", runtime)
    monkeypatch.setattr(pi_config, "PI_NODE_BINARY", node)
    pi_config._validate_pi_runtime.cache_clear()

    with pytest.raises(pi_config.PiRuntimeError, match="Node 运行时版本错误"):
        pi_config.validate_pi_worker_runtime(
            {"binary": str(runtime), "model": "test-model"}
        )


def test_runner_rejects_an_unpinned_pi_worker_before_stage_start(tmp_path) -> None:
    worker = Worker(
        name="worker-a",
        worker_type="pi",
        capabilities=["explore"],
        max_running=1,
        priority=0,
        config={"binary": str(tmp_path / "other-pi"), "model": "test-model"},
        disabled=False,
        created_at="2026-07-28T00:00:00Z",
        updated_at="2026-07-28T00:00:00Z",
    )
    runner = AxiomRunner(object())
    try:
        with pytest.raises(pi_config.PiRuntimeError, match="必须使用版本化运行时"):
            runner._default_executor(worker, SimpleNamespace(kind="explore"))
    finally:
        runner.close()
