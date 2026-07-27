from __future__ import annotations

from pathlib import Path

import pytest

from axiom.dispatcher.bootstrap import BootstrapExecutionTimeout
from axiom.dispatcher.pi import PiBootstrapExecutor, _reason_tool_names
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


def test_pi_adapter_returns_the_final_assistant_text(tmp_path) -> None:
    script = make_script(
        tmp_path,
        "printf '# 资产' > \"$AXIOM_RUN_DIR/asset_inventory.md\"\n"
        "printf '# 线索' > \"$AXIOM_RUN_DIR/initial_leads.md\"\n"
        "printf '# 无漏洞' > \"$AXIOM_RUN_DIR/findings.md\"\n"
        "printf 'FINISH'",
    )
    run_dir = tmp_path / "run"

    output = PiBootstrapExecutor(pi_worker(script)).execute(
        execution_input(run_dir),
        lease_active=lambda: True,
    )

    assert output == "FINISH"


def test_pi_adapter_materializes_custom_provider_and_shared_skill_paths(tmp_path) -> None:
    security_bin = tmp_path / "security-bin"
    security_bin.mkdir()
    security_httpx = security_bin / "httpx"
    security_httpx.write_text("#!/bin/sh\necho projectdiscovery-httpx\n", encoding="utf-8")
    security_httpx.chmod(0o755)
    script = make_script(
        tmp_path,
        "test -f \"$PI_CODING_AGENT_DIR/models.json\"\n"
        "test \"$AXIOM_RUN_DIR\" = \"$PI_CODING_AGENT_DIR\"\n"
        "test \"$(command -v axiom)\" = \"$AXIOM_RUN_DIR/.axiom-bin/axiom\"\n"
        "test \"$(command -v python3)\" = \"$AXIOM_RUN_DIR/.axiom-bin/python3\"\n"
        "test \"$(command -v httpx)\" = \"$EXPECTED_HTTPX\"\n"
        "python3 -c 'import sys; assert sys.prefix.endswith(\"/.venv\")'\n"
        "grep -q '\"baseUrl\":\"https://models.example/v1\"' \"$PI_CODING_AGENT_DIR/models.json\"\n"
        "printf '%s\\n' \"$@\" | grep -q '/shared/skills/httpx'\n"
        "printf '# 资产' > \"$AXIOM_RUN_DIR/asset_inventory.md\"\n"
        "printf '# 线索' > \"$AXIOM_RUN_DIR/initial_leads.md\"\n"
        "printf '# 无漏洞' > \"$AXIOM_RUN_DIR/findings.md\"\n"
        "printf 'FINISH'",
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
                "environment": {
                    "PATH": "/usr/bin:/bin",
                    "AXIOM_TOOL_PATH": str(security_bin),
                    "EXPECTED_HTTPX": str(security_httpx),
                },
            }
        }
    )
    run_dir = tmp_path / "run"
    input_value = execution_input(run_dir).model_copy(
        update={"skill_directories": ["/shared/skills/httpx"]}
    )

    output = PiBootstrapExecutor(worker).execute(input_value, lease_active=lambda: True)

    assert output == "FINISH"
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


def test_pi_adapter_streams_session_tool_activity_while_process_runs(tmp_path) -> None:
    script = make_script(
        tmp_path,
        "mkdir -p \"$PI_CODING_AGENT_DIR/sessions\"\n"
        "cat >> \"$PI_CODING_AGENT_DIR/sessions/live.jsonl\" <<'JSON'\n"
        '{"type":"message","message":{"role":"assistant","content":[{"type":"toolCall","id":"call-1","name":"bash","arguments":{"command":"httpx -u https://a.example.com"}}]}}\n'
        "JSON\n"
        "sleep 0.4\n"
        "printf '# 资产' > \"$AXIOM_RUN_DIR/asset_inventory.md\"\n"
        "printf '# 线索' > \"$AXIOM_RUN_DIR/initial_leads.md\"\n"
        "printf '# 无漏洞' > \"$AXIOM_RUN_DIR/findings.md\"\n"
        "printf 'FINISH'",
    )
    activities = []

    output = PiBootstrapExecutor(
        pi_worker(script),
        activity_reporter=activities.append,
    ).execute(execution_input(tmp_path / "run"), lease_active=lambda: True)

    assert output == "FINISH"
    assert activities
    assert activities[0].lease_id == "lease_test"
    assert activities[0].activity_tool == "httpx"
    assert activities[0].activity_summary == "正在执行 httpx 服务探测与指纹识别"


def test_reason_executor_always_keeps_bash_available_for_axiom_cli() -> None:
    assert _reason_tool_names(["read", "write"]) == ["read", "write", "bash"]
    assert _reason_tool_names(["read", "bash"]) == ["read", "bash"]
