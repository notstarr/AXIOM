from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from axiom.dispatcher.runner import AxiomRunner
from axiom.modules.workers import pi_config
from axiom.modules.workers.schemas import Worker


def _version_script(path: Path, version: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{version}'\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _pi_runtime(
    root: Path,
    *,
    package_name: str = "@earendil-works/pi-coding-agent",
    version: str = pi_config.PI_RUNTIME_VERSION,
    bin_entry: object = None,
    launcher: str = "#!/bin/sh\nexit 0\n",
    create_cli_entry: bool = True,
) -> Path:
    runtime = root / "bin" / "pi"
    runtime.parent.mkdir(parents=True, exist_ok=True)
    runtime.write_text(launcher, encoding="utf-8")
    runtime.chmod(0o755)

    package_root = (
        root / "node_modules" / "@earendil-works" / "pi-coding-agent"
    )
    package_root.mkdir(parents=True, exist_ok=True)
    resolved_bin_entry = {"pi": "dist/cli.js"} if bin_entry is None else bin_entry
    (package_root / "package.json").write_text(
        json.dumps(
            {
                "name": package_name,
                "version": version,
                "bin": resolved_bin_entry,
            }
        ),
        encoding="utf-8",
    )
    if (
        create_cli_entry
        and isinstance(resolved_bin_entry, dict)
        and isinstance(resolved_bin_entry.get("pi"), str)
    ):
        cli_entry = package_root / resolved_bin_entry["pi"]
        cli_entry.parent.mkdir(parents=True, exist_ok=True)
        cli_entry.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        cli_entry.chmod(0o755)
    return runtime


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


def test_sdk_base_url_follows_per_provider_sdk_conventions() -> None:
    # Anthropic SDK appends /v1/messages itself: it needs the bare root.
    assert (
        pi_config.sdk_base_url("anthropic-messages", "https://api.anthropic.com")
        == "https://api.anthropic.com"
    )
    # OpenAI-family SDKs post bare resource paths: /v1 belongs in the baseURL.
    assert (
        pi_config.sdk_base_url("openai-completions", "https://api.openai.com")
        == "https://api.openai.com/v1"
    )
    assert (
        pi_config.sdk_base_url("openai-responses", "https://api.openai.com")
        == "https://api.openai.com/v1"
    )
    # Legacy /v1-suffixed input is idempotent, never doubled.
    assert (
        pi_config.sdk_base_url("openai-completions", "https://api.openai.com/v1")
        == "https://api.openai.com/v1"
    )
    assert (
        pi_config.sdk_base_url("anthropic-messages", "https://api.anthropic.com/v1")
        == "https://api.anthropic.com"
    )


def test_upstream_request_url_yields_single_v1_prefix() -> None:
    assert (
        pi_config.upstream_request_url("anthropic-messages", "https://h.test", "v1/messages")
        == "https://h.test/v1/messages"
    )
    assert (
        pi_config.upstream_request_url("openai-completions", "https://h.test", "chat/completions")
        == "https://h.test/v1/chat/completions"
    )
    assert (
        pi_config.upstream_request_url("openai-responses", "https://h.test", "/responses")
        == "https://h.test/v1/responses"
    )
    assert (
        pi_config.upstream_request_url("openai-completions", "https://h.test", "/models")
        == "https://h.test/v1/models"
    )
    # A /v1-suffixed base_url does not double the prefix.
    assert (
        pi_config.upstream_request_url("anthropic-messages", "https://h.test/v1", "v1/messages")
        == "https://h.test/v1/messages"
    )


def test_pi_worker_config_persists_provider_root_without_v1() -> None:
    settings = pi_config.PiWorkerConfig.model_validate(
        {"base_url": "https://api.example.com/v1/", "provider_api": "anthropic-messages"}
    )
    assert settings.base_url == "https://api.example.com"


def test_validate_pi_worker_runtime_checks_pinned_pi_and_node(monkeypatch, tmp_path) -> None:
    runtime = _pi_runtime(tmp_path / "runtime")
    node = _version_script(tmp_path / "node", pi_config.PI_NODE_VERSION)
    monkeypatch.setattr(pi_config, "PI_RUNTIME_BINARY", runtime)
    monkeypatch.setattr(pi_config, "PI_NODE_BINARY", node)
    pi_config._validate_pi_runtime.cache_clear()

    settings = pi_config.validate_pi_worker_runtime(
        {"binary": "pi", "model": "test-model"}
    )

    assert settings.binary == str(runtime)


def test_pi_runtime_validation_uses_local_metadata_without_starting_cli(monkeypatch, tmp_path) -> None:
    marker = tmp_path / "pi-started"
    runtime = _pi_runtime(
        tmp_path,
        launcher=f"#!/bin/sh\nprintf '%s' started > '{marker}'\nsleep 60\n",
    )
    node = _version_script(tmp_path / "node", pi_config.PI_NODE_VERSION)
    monkeypatch.setattr(pi_config, "PI_RUNTIME_BINARY", runtime)
    monkeypatch.setattr(pi_config, "PI_NODE_BINARY", node)
    pi_config._validate_pi_runtime.cache_clear()

    pi_config.validate_pi_worker_runtime({"binary": "pi", "model": "test-model"})

    assert not marker.exists()


def test_validate_pi_worker_runtime_rejects_unpinned_binary(monkeypatch, tmp_path) -> None:
    runtime = _pi_runtime(tmp_path / "runtime")
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
    runtime = _pi_runtime(tmp_path / "runtime")
    node = _version_script(tmp_path / "node", "v22.15.0")
    monkeypatch.setattr(pi_config, "PI_RUNTIME_BINARY", runtime)
    monkeypatch.setattr(pi_config, "PI_NODE_BINARY", node)
    pi_config._validate_pi_runtime.cache_clear()

    with pytest.raises(pi_config.PiRuntimeError, match="Node 运行时版本错误"):
        pi_config.validate_pi_worker_runtime(
            {"binary": str(runtime), "model": "test-model"}
        )


@pytest.mark.parametrize(
    ("runtime_kwargs", "message"),
    [
        ({"package_name": "not-pi"}, "Pi 运行时包错误"),
        ({"version": "0.80.3"}, "Pi 运行时版本错误"),
        ({"bin_entry": {}}, "Pi 运行时元数据缺少 bin.pi"),
        ({"bin_entry": {"pi": "../../outside.js"}}, "Pi CLI 入口越界"),
        (
            {"bin_entry": {"pi": "dist/missing.js"}, "create_cli_entry": False},
            "Pi CLI 运行时不存在",
        ),
    ],
)
def test_validate_pi_worker_runtime_rejects_invalid_package_metadata(
    monkeypatch,
    tmp_path,
    runtime_kwargs,
    message,
) -> None:
    runtime = _pi_runtime(tmp_path / "runtime", **runtime_kwargs)
    node = _version_script(tmp_path / "node", pi_config.PI_NODE_VERSION)
    monkeypatch.setattr(pi_config, "PI_RUNTIME_BINARY", runtime)
    monkeypatch.setattr(pi_config, "PI_NODE_BINARY", node)
    pi_config._validate_pi_runtime.cache_clear()

    with pytest.raises(pi_config.PiRuntimeError, match=message):
        pi_config.validate_pi_worker_runtime(
            {"binary": str(runtime), "model": "test-model"}
        )


def test_validate_pi_worker_runtime_rejects_non_object_package_metadata(
    monkeypatch,
    tmp_path,
) -> None:
    runtime_root = tmp_path / "runtime"
    runtime = _pi_runtime(runtime_root)
    package = (
        runtime_root
        / "node_modules"
        / "@earendil-works"
        / "pi-coding-agent"
        / "package.json"
    )
    package.write_text("[]\n", encoding="utf-8")
    node = _version_script(tmp_path / "node", pi_config.PI_NODE_VERSION)
    monkeypatch.setattr(pi_config, "PI_RUNTIME_BINARY", runtime)
    monkeypatch.setattr(pi_config, "PI_NODE_BINARY", node)
    pi_config._validate_pi_runtime.cache_clear()

    with pytest.raises(pi_config.PiRuntimeError, match="Pi 运行时元数据格式错误"):
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
