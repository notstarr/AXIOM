from __future__ import annotations

from pathlib import Path

import pytest

from axiom.dispatcher.bootstrap import BootstrapExecutionTimeout
from axiom.dispatcher.pi import PiBootstrapExecutor
from axiom.modules.execution.schemas import ExecutionInput
from axiom.modules.workers.schemas import Worker


def pi_worker(binary: Path) -> Worker:
    return Worker(
        name="worker-a",
        worker_type="pi",
        capabilities=["bootstrap"],
        max_running=1,
        priority=0,
        config={"binary": str(binary), "model": "test-model"},
        disabled=False,
        created_at="2026-07-23T00:00:00Z",
        updated_at="2026-07-23T00:00:00Z",
    )


def execution_input(run_dir: Path) -> ExecutionInput:
    run_dir.mkdir(parents=True)
    return ExecutionInput(
        task_id="t_001",
        intent_id="i001",
        lease_id="lease_test",
        worker_name="worker-a",
        origin="https://a.example.com",
        goal="完成测试",
        hints=[],
        scope={"enabled": True, "targets": ["a.example.com"], "exclude": []},
        run_dir=str(run_dir),
        execute_timeout_seconds=2,
        conclude_timeout_seconds=1,
    )


def make_script(tmp_path, body: str) -> Path:
    path = tmp_path / "fake-pi"
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_pi_adapter_reads_the_bundle_from_run_directory(tmp_path) -> None:
    script = make_script(
        tmp_path,
        "cat > \"$AXIOM_RUN_DIR/bootstrap_artifacts.json\" <<'JSON'\n"
        "{\"asset_inventory\":\"# 资产\",\"initial_leads\":\"# 线索\",\"findings\":\"# 无漏洞\"}\n"
        "JSON\nprintf 'Bootstrap summary'",
    )
    run_dir = tmp_path / "run"

    output = PiBootstrapExecutor(pi_worker(script)).execute(
        execution_input(run_dir),
        lease_active=lambda: True,
    )

    assert output["summary"] == "Bootstrap summary"
    assert output["artifacts"].asset_inventory == "# 资产\n"


def test_pi_adapter_materializes_custom_provider_and_shared_skill_paths(tmp_path) -> None:
    script = make_script(
        tmp_path,
        "test -f \"$PI_CODING_AGENT_DIR/models.json\"\n"
        "grep -q '\"baseUrl\":\"https://models.example/v1\"' \"$PI_CODING_AGENT_DIR/models.json\"\n"
        "printf '%s\\n' \"$@\" | grep -q '/shared/skills/httpx'\n"
        "cat > \"$AXIOM_RUN_DIR/bootstrap_artifacts.json\" <<'JSON'\n"
        "{\"asset_inventory\":\"# 资产\",\"initial_leads\":\"# 线索\",\"findings\":\"# 无漏洞\"}\n"
        "JSON\nprintf 'Bootstrap summary'",
    )
    worker = pi_worker(script).model_copy(
        update={
            "config": {
                "binary": str(script),
                "provider": "axiom",
                "model": "test-model",
                "base_url": "https://models.example/v1",
                "provider_api": "openai-completions",
                "api_key": "test-key",
            }
        }
    )
    run_dir = tmp_path / "run"
    input_value = execution_input(run_dir).model_copy(
        update={"skill_directories": ["/shared/skills/httpx"]}
    )

    output = PiBootstrapExecutor(worker).execute(input_value, lease_active=lambda: True)

    assert output["summary"] == "Bootstrap summary"
    models = (run_dir / "models.json").read_text(encoding="utf-8")
    assert '"providers":{"axiom"' in models
    assert '"apiKey":"test-key"' in models


def test_pi_adapter_converts_process_timeout_to_closeout_signal(tmp_path) -> None:
    script = make_script(tmp_path, "sleep 5")

    with pytest.raises(BootstrapExecutionTimeout):
        PiBootstrapExecutor(pi_worker(script)).execute(
            execution_input(tmp_path / "run"),
            lease_active=lambda: True,
        )
