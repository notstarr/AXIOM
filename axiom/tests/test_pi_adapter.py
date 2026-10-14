from __future__ import annotations

import json
from pathlib import Path

import pytest

from axiom.dispatcher.bootstrap import BootstrapExecutionTimeout, TransientWorkerError
from axiom.dispatcher.browser_broker import BROWSER_TOOL_NAMES, BrowserLeaseHandle
from axiom.dispatcher.pi import (
    CONTEXT_ATTENTION_SYSTEM_PROMPT,
    NO_BASH_CONTEXT_ATTENTION_SYSTEM_PROMPT,
    PiBootstrapExecutor,
    PiExploreExecutor,
    PiReasonExecutor,
    PiReviewExecutor,
    PromptRenderer,
    _ExploreArtifactProgressProbe,
    _explore_soft_reminder_timing,
    _intent_targets_text,
    _reason_target_guidance,
    _scope_guidance,
)
from axiom.dispatcher.process import ProcessResult
from axiom.modules.execution.schemas import ExecutionInput
from axiom.modules.phase_capabilities.builtins import native_tool_names
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
    context_manifest_path = run_dir / "context.json"
    context_manifest_path.write_text("{}", encoding="utf-8")
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
        context_manifest_path=str(context_manifest_path),
    )


def make_script(tmp_path, body: str) -> Path:
    path = tmp_path / "fake-pi"
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_explore_progress_probe_bounded_closeout_after_stale_artifacts(
    tmp_path: Path, monkeypatch
) -> None:
    ticks = iter([1.0, 2.0, 3.0, 34.0])
    monkeypatch.setattr("axiom.dispatcher.pi.time.monotonic", lambda: next(ticks))

    probe = _ExploreArtifactProgressProbe(
        str(tmp_path / "run"),
        timeout_seconds=30,
        scan_interval=0.2,
    )

    assert probe() is None
    assert probe() is None
    assert probe() == "explore_no_progress"


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


def test_pi_json_terminal_provider_error_is_transient_even_with_zero_exit(tmp_path) -> None:
    stdout_path = tmp_path / "stdout.log"
    stderr_path = tmp_path / "stderr.log"
    stdout = "\n".join(
        [
            json.dumps(
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "content": [],
                        "stopReason": "error",
                        "errorMessage": "HTTP 429: quota exhausted",
                    },
                }
            ),
            json.dumps(
                {
                    "type": "auto_retry_end",
                    "success": False,
                    "attempt": 3,
                    "finalError": "HTTP 429: quota exhausted",
                }
            ),
            json.dumps({"type": "agent_settled"}),
        ]
    )
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")
    result = ProcessResult(
        returncode=0,
        stdout=stdout,
        stderr="",
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )

    with pytest.raises(TransientWorkerError, match="HTTP 429: quota exhausted"):
        PiBootstrapExecutor._raise_on_process_failure(result)


def test_pi_json_recovered_provider_error_does_not_mask_final_success(tmp_path) -> None:
    stdout_path = tmp_path / "stdout.log"
    stderr_path = tmp_path / "stderr.log"
    stdout = "\n".join(
        [
            json.dumps(
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "content": [],
                        "stopReason": "error",
                        "errorMessage": "HTTP 503: unavailable",
                    },
                }
            ),
            json.dumps({"type": "auto_retry_start", "attempt": 1}),
            json.dumps(
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "FINISH"}],
                        "stopReason": "stop",
                    },
                }
            ),
            json.dumps({"type": "auto_retry_end", "success": True, "attempt": 1}),
        ]
    )
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")
    result = ProcessResult(
        returncode=0,
        stdout=stdout,
        stderr="",
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )

    PiBootstrapExecutor._raise_on_process_failure(result)

    assert PiBootstrapExecutor._last_assistant_text(result) == "FINISH"


def test_pi_json_empty_output_without_provider_error_is_not_process_failure(tmp_path) -> None:
    stdout_path = tmp_path / "stdout.log"
    stderr_path = tmp_path / "stderr.log"
    stdout_path.write_text("", encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")
    result = ProcessResult(
        returncode=0,
        stdout="",
        stderr="",
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )

    PiBootstrapExecutor._raise_on_process_failure(result)

    assert PiBootstrapExecutor._last_assistant_text(result) == ""


def test_pi_rpc_stream_stall_is_a_transient_worker_error(tmp_path) -> None:
    stdout_path = tmp_path / "stdout.log"
    stderr_path = tmp_path / "stderr.log"
    stdout_path.write_text("", encoding="utf-8")
    stderr_path.write_text("pi_rpc_stream_stalled\nidle_seconds=120.001\n", encoding="utf-8")
    result = ProcessResult(
        returncode=1,
        stdout="",
        stderr=stderr_path.read_text(encoding="utf-8"),
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        rpc_stream_stalled=True,
    )

    with pytest.raises(TransientWorkerError, match="pi_rpc_stream_stalled"):
        PiBootstrapExecutor._raise_on_process_failure(result)


def test_bootstrap_rejects_missing_context_manifest(tmp_path) -> None:
    input_value = execution_input(tmp_path / "run").model_copy(
        update={"context_manifest_path": None}
    )

    with pytest.raises(TransientWorkerError, match="Bootstrap Context manifest"):
        PiBootstrapExecutor(pi_worker(tmp_path / "unused-pi")).execute(
            input_value,
            lease_active=lambda: True,
        )


def test_pi_json_and_rpc_modes_append_the_shared_context_attention_rule(tmp_path) -> None:
    executor = PiBootstrapExecutor(pi_worker(tmp_path / "unused-pi"))
    executor._session_id = "session-test"

    for mode in ("json", "rpc"):
        argv = executor._build_argv(
            "测试提示",
            tmp_path / "sessions",
            ["read", "bash"],
            mode=mode,
        )
        index = argv.index("--append-system-prompt")
        assert argv[index + 1] == CONTEXT_ATTENTION_SYSTEM_PROMPT
        assert "hints.json、knowledge.json、vulnerabilities.json" in argv[index + 1]
        assert "axiom skill path whatisvul" in argv[index + 1]
        assert "axiom skill doctor whatisvul" in argv[index + 1]
        assert "不得用记忆、常识或其它 Skill 替代" in argv[index + 1]


def test_pi_adapter_uses_no_bash_context_rule_for_bootstrap_conclude(tmp_path) -> None:
    executor = PiBootstrapExecutor(pi_worker(tmp_path / "unused-pi"))
    executor._session_id = "session-test"

    argv = executor._build_argv(
        "收口提示",
        tmp_path / "sessions",
        native_tool_names("bootstrap", "conclude"),
    )

    index = argv.index("--append-system-prompt")
    assert argv[index + 1] == NO_BASH_CONTEXT_ATTENTION_SYSTEM_PROMPT
    assert "本收口没有 Bash" in argv[index + 1]
    assert "不得调用 axiom skill path/doctor" in argv[index + 1]
    assert "复用当前会话执行阶段已经读取的 whatisvul/SKILL.md" in argv[index + 1]


@pytest.mark.parametrize("mode", ["json", "rpc"])
def test_pi_adapter_loads_only_selected_axiom_tool_extensions(
    tmp_path: Path, mode: str
) -> None:
    executor = PiBootstrapExecutor(pi_worker(tmp_path / "unused-pi"))
    package_extension = tmp_path / "managed_probe" / "index.ts"
    package_extension.parent.mkdir()
    package_extension.write_text("export default function () {}\n", encoding="utf-8")

    argv = executor._build_argv(
        "测试提示",
        tmp_path / "sessions",
        ["read", "bash"],
        mode=mode,
        axiom_tool_extensions=[str(package_extension)],
        axiom_tool_keys=["managed_probe"],
    )

    assert argv[argv.index("--tools") + 1] == "read,bash,managed_probe"
    extensions = [argv[index + 1] for index, value in enumerate(argv) if value == "--extension"]
    assert extensions[0] == str(package_extension)
    assert extensions[-1].endswith("/extensions/axiom-tool-activation/index.ts")
    assert "unselected_probe" not in " ".join(argv)
    assert ("-p" in argv) is (mode == "json")


@pytest.mark.parametrize("mode", ["json", "rpc"])
def test_pi_adapter_merges_only_snapshotted_mcp_tools_into_the_stage_allowlist(
    tmp_path: Path, mode: str
) -> None:
    executor = PiBootstrapExecutor(pi_worker(tmp_path / "unused-pi"))
    argv = executor._build_argv(
        "测试提示",
        tmp_path / "sessions",
        ["read", "write", "bash", "edit"],
        mode=mode,
        mcp_tool_names=["mcp__demo__selected__12345678"],
    )

    allowed = argv[argv.index("--tools") + 1].split(",")
    assert allowed == ["read", "write", "bash", "edit", "mcp__demo__selected__12345678"]
    assert "mcp__demo__sibling__87654321" not in allowed
    extensions = [
        argv[index + 1]
        for index, value in enumerate(argv)
        if value == "--extension"
    ]
    assert extensions == [
        str(
            Path(__file__).parents[1]
            / "src/axiom/dispatcher/extensions/axiom-mcp-tools/index.ts"
        )
    ]


def test_pi_adapter_loads_native_mcp_extension_and_hides_auxiliary_tools(tmp_path: Path) -> None:
    executor = PiBootstrapExecutor(pi_worker(tmp_path / "unused-pi"))
    argv = executor._build_argv(
        "测试提示",
        tmp_path / "sessions",
        ["read", "write"],
        mode="json",
        mcp_mode="pi_native",
        mcp_tool_names=["mcp__demo__selected"],
        mcp_server_names=["demo"],
    )

    extensions = [
        argv[index + 1]
        for index, value in enumerate(argv)
        if value == "--extension"
    ]
    assert extensions == [
        str(
            Path(__file__).parents[1]
            / "src/axiom/dispatcher/extensions/axiom-native-mcp/index.ts"
        )
    ]
    assert argv[argv.index("--exclude-tools") + 1] == (
        "codemode,tool_search,list_mcp_resources,list_mcp_resource_templates,read_mcp_resource"
    )
    assert "mcp__demo__*" in argv[argv.index("--tools") + 1].split(",")
    assert "--no-approve" in argv


def test_pi_adapter_does_not_load_activation_without_selected_tools(tmp_path: Path) -> None:
    executor = PiBootstrapExecutor(pi_worker(tmp_path / "unused-pi"))

    argv = executor._build_argv(
        "测试提示",
        tmp_path / "sessions",
        ["read"],
        axiom_tool_extensions=[],
        axiom_tool_keys=[],
    )

    assert "axiom-tool-activation" not in " ".join(argv)


@pytest.mark.parametrize(
    ("enabled", "level", "expected_level"),
    [(False, "high", "off"), (True, "high", "high"), (True, "max", "max")],
)
def test_pi_adapter_passes_explicit_thinking_state_to_pi(
    tmp_path, enabled: bool, level: str, expected_level: str
) -> None:
    executor = PiBootstrapExecutor(
        pi_worker(tmp_path / "unused-pi").model_copy(
            update={
                "config": {
                    "model": "test-model",
                    "base_url": "https://models.example/v1",
                    "context_window": 500_000,
                    "thinking_enabled": enabled,
                    "thinking_level": level,
                }
            }
        )
    )
    argv = executor._build_argv("测试", tmp_path / "sessions", ["read"])
    thinking_index = argv.index("--thinking")
    assert argv[thinking_index + 1] == expected_level

    agent_dir = tmp_path / "run"
    executor._write_models_file(agent_dir)
    model = json.loads((agent_dir / "models.json").read_text())["providers"]["axiom"]["models"][0]
    assert model["contextWindow"] == 500_000
    assert model["reasoning"] is enabled
    if level == "max" and enabled:
        assert model["thinkingLevelMap"] == {"max": "max"}


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
        "test \"$PI_MCP_ENABLED\" = \"false\"\n"
        "test -z \"$PI_MCP_ALLOWED_TOOLS\"\n"
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
                "PI_MCP_ENABLED": "true",
                "PI_MCP_ALLOWED_TOOLS": "legacy-sibling",
            },
            }
        }
    )
    run_dir = tmp_path / "run"
    input_value = execution_input(run_dir).model_copy(
        update={
            "skill_directories": ["/shared/skills/httpx"],
            "capability_environment": {
                "PI_MCP_ENABLED": "false",
                "PI_MCP_CONFIG_JSON": '{"mcpServers":{}}',
                "PI_MCP_ALLOWED_TOOLS": "",
            },
        }
    )

    output = PiBootstrapExecutor(worker).execute(input_value, lease_active=lambda: True)

    assert output == "FINISH"
    models = (run_dir / "models.json").read_text(encoding="utf-8")
    assert '"providers":{"axiom"' in models
    assert '"apiKey":"test-key"' in models


def test_pi_adapter_injects_discovered_nmap_data_directory(
    tmp_path, monkeypatch
) -> None:
    home = tmp_path / "home"
    data_dir = home / ".local" / "share" / "nmap"
    for relative in (
        "nmap-services",
        "nmap-protocols",
        "nse_main.lua",
        "scripts/script.db",
    ):
        path = data_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("test", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    script = make_script(
        tmp_path,
        "test \"$NMAPDIR\" = \"$EXPECTED_NMAPDIR\"\n"
        "printf '# 资产' > \"$AXIOM_RUN_DIR/asset_inventory.md\"\n"
        "printf '# 线索' > \"$AXIOM_RUN_DIR/initial_leads.md\"\n"
        "printf '# 无漏洞' > \"$AXIOM_RUN_DIR/findings.md\"\n"
        "printf 'FINISH'",
    )
    worker = pi_worker(script).model_copy(
        update={
            "config": {
                "binary": str(script),
                "model": "test-model",
                "environment": {"EXPECTED_NMAPDIR": str(data_dir.resolve())},
            }
        }
    )

    output = PiBootstrapExecutor(worker).execute(
        execution_input(tmp_path / "run"), lease_active=lambda: True
    )

    assert output == "FINISH"


def test_pi_adapter_does_not_call_removed_browser_cli_cleanup(tmp_path: Path) -> None:
    browser_skill = tmp_path / "browser-control"
    browser_skill.mkdir(parents=True)
    (browser_skill / "SKILL.md").write_text(
        "---\nname: browser-control\n---\n", encoding="utf-8"
    )
    script = make_script(
        tmp_path,
        "printf '# 资产' > \"$AXIOM_RUN_DIR/asset_inventory.md\"\n"
        "printf '# 线索' > \"$AXIOM_RUN_DIR/initial_leads.md\"\n"
        "printf '# 无漏洞' > \"$AXIOM_RUN_DIR/findings.md\"\n"
        "printf 'FINISH'",
    )
    worker = pi_worker(script).model_copy(
        update={
            "config": {
                "binary": str(script),
                "model": "test-model",
            }
        }
    )
    input_value = execution_input(tmp_path / "run").model_copy(
        update={"skill_directories": [str(browser_skill)]}
    )

    output = PiBootstrapExecutor(worker).execute(
        input_value, lease_active=lambda: True
    )

    assert output == "FINISH"

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


def test_native_tool_contracts_are_fixed_by_phase_and_run_mode() -> None:
    assert native_tool_names("bootstrap", "execute") == [
        "read",
        "write",
        "edit",
        "bash",
    ]
    assert native_tool_names("bootstrap", "conclude") == ["read", "write", "edit"]
    assert native_tool_names("reason", "execute") == [
        "read",
        "write",
        "edit",
        "bash",
        "web_search",
        "fetch_web_content",
    ]
    assert native_tool_names("reason", "conclude") == [
        "read",
        "write",
        "edit",
        "bash",
        "web_search",
        "fetch_web_content",
    ]
    assert native_tool_names("explore", "execute") == [
        "read",
        "write",
        "edit",
        "bash",
        "web_search",
        "fetch_web_content",
    ]
    assert native_tool_names("explore", "conclude") == [
        "read",
        "write",
        "edit",
        "bash",
        "web_search",
        "fetch_web_content",
    ]
    assert native_tool_names("plan", "execute") == [
        "read",
        "write",
        "edit",
        "bash",
        "web_search",
        "fetch_web_content",
    ]
    assert native_tool_names("plan", "conclude") == [
        "read",
        "write",
        "edit",
        "bash",
        "web_search",
        "fetch_web_content",
    ]
    assert native_tool_names("review", "execute") == ["read", "write", "edit", "bash"]
    assert native_tool_names("review", "conclude") == ["read", "write", "edit", "bash"]


@pytest.mark.parametrize(
    ("profile", "marker"),
    [
        ("bootstrap", "Bootstrap 完成后的首次规划"),
        ("progress", "自动增量规划"),
        ("human", "人工触发或人工提示"),
        ("continuation", "人工继续任务后的新 Goal 规划"),
        ("default", "默认安全策略"),
    ],
)
def test_reason_executor_injects_only_the_code_selected_prompt_profile(
    tmp_path: Path, profile: str, marker: str
) -> None:
    run_dir = tmp_path / "reason-run"
    input_value = execution_input(run_dir).model_copy(
        update={
            "intent_kind": "reason",
            "intent_id": None,
            "reason_run_id": "rr000001",
            "reason_prompt_profile": profile,
            "context_manifest_path": str(run_dir / "context.json"),
        }
    )
    prompt = PiReasonExecutor(pi_worker(tmp_path / "unused-pi"))._build_reason_prompt(
        input_value
    )

    assert "所有 ReasonRun 共用的主提示词" in prompt
    assert f"由 AXIOM 代码固定注入：{profile}" in prompt
    assert marker in prompt
    # The selected profile is appended by code; the main prompt must not ask the
    # model to discover or choose a different profile.
    assert "不得自行选择、读取或叠加其它 Reason 子提示词" in prompt


def test_reason_prompt_defaults_profile_for_pre_restart_execution_input(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "reason-run"
    input_value = execution_input(run_dir).model_copy(
        update={
            "intent_kind": "reason",
            "intent_id": None,
            "reason_run_id": "rr000001",
            "context_manifest_path": str(run_dir / "context.json"),
        }
    )
    input_value.__dict__.pop("reason_prompt_profile", None)

    prompt = PiReasonExecutor(pi_worker(tmp_path / "unused-pi"))._build_reason_prompt(
        input_value
    )

    assert "由 AXIOM 代码固定注入：default" in prompt
    assert "默认安全策略" in prompt


def test_reason_conclude_prompt_is_not_profile_split() -> None:
    prompt = PromptRenderer().render(
        "reason_conclude.md",
        {
            "task_id": "t_001",
            "reason_run_id": "rr000001",
            "workspace_id": "rw_000001",
            "run_dir": "/tmp/run",
            "target_guidance": _reason_target_guidance({}),
        },
    )

    assert "reason_bootstrap.md" not in prompt
    assert "reason_progress.md" not in prompt
    assert "reason_continuation.md" not in prompt


def test_reason_prompt_requires_small_todos_and_intents_without_fixed_budget_example() -> None:
    prompt = PromptRenderer().render(
        "reason.md",
        {
            "task_id": "t_001",
            "reason_run_id": "rr000001",
            "intent_description": "规划下一轮",
            "source_fact_ids": '["f002", "f003"]',
            "origin": "a.example.com",
            "goal": "完成测试",
            "target_guidance": _reason_target_guidance({}),
            "context_manifest_path": "/tmp/context.json",
            "run_dir": "/tmp/run",
            "skill_directories": "- /tmp/skills",
        },
    )

    assert "axiom reason intent assess" in prompt
    assert "axiom reason goal assess" in prompt
    assert "source_fact_ids" in prompt
    assert "whatisvul" in prompt
    assert "自建自测闭环" in prompt
    assert "FINISH" in prompt
    assert "--execute-seconds 600 --conclude-seconds 120" not in prompt


def test_reason_conclude_prompt_keeps_strict_planning_gate() -> None:
    prompt = PromptRenderer().render(
        "reason_conclude.md",
        {
            "task_id": "t_001",
            "reason_run_id": "rr000001",
            "workspace_id": "rw_000001",
            "run_dir": "/tmp/run",
            "target_guidance": _reason_target_guidance({}),
        },
    )

    assert "收口" in prompt
    assert "axiom reason status" in prompt
    assert "axiom reason goal assess" in prompt
    assert "FINISH" in prompt
    assert "0` 个执行意图" not in prompt
    assert "提交 0 Intent" not in prompt
    assert "至少保留一个可执行" not in prompt


def test_bootstrap_prompt_receives_manifest_and_marks_context_as_injected() -> None:
    prompt = PromptRenderer().render(
        "bootstrap.md",
        {
            "task_id": "t_001",
            "intent_id": "i001",
            "origin": "a.example.com",
            "goal": "完成测试",
            "scope_guidance": _scope_guidance({}),
            "context_manifest_path": "/tmp/context.json",
            "run_dir": "/tmp/run",
            "skill_directories": "- /tmp/skills",
        },
    )

    assert "上下文清单（Context Manifest）：`/tmp/context.json`" in prompt
    assert "已由 AXIOM 直接注入" in prompt
    assert "不要重复读取已注入内容" in prompt


def test_bootstrap_prompt_routes_one_target_profile_and_locks_target_identity() -> None:
    prompt = PromptRenderer().render(
        "bootstrap.md",
        {
            "task_id": "t_001",
            "intent_id": "i001",
            "origin": "agentpark小程序",
            "goal": "完成测试",
            "scope_guidance": _scope_guidance({}),
            "context_manifest_path": "/tmp/context.json",
            "run_dir": "/tmp/run",
            "skill_directories": "- /tmp/skills/miniapp",
        },
    )

    assert "$AXIOM_PYTHON" in prompt
    assert "import axiom" in prompt
    assert "bootstrap_web.md" in prompt
    assert "bootstrap_miniapp.md" in prompt
    assert "bootstrap_app.md" in prompt
    assert "每轮最多读取一份类型子提示词" in prompt
    assert "不得读取任何类型子提示词" in prompt
    assert "WebView/H5 仍属于该小程序" in prompt
    assert "不要默认按 Web" in prompt
    assert "不得因为发现其它缓存应用" in prompt
    assert "JS 资源收集" in prompt
    assert "认证前后资源" in prompt
    assert "credential-refresh" in prompt
    assert "业务全景图" in prompt
    assert "whatisvul" in prompt
    assert "asset_inventory.md" in prompt
    assert "initial_leads.md" in prompt
    assert "目标类型：Web / 小程序 / App / 尚未确认" in prompt
    assert "当前目标身份" in prompt
    assert "身份判断依据" in prompt
    assert "FINISH" in prompt
    assert len(prompt.splitlines()) <= 75


@pytest.mark.parametrize(
    ("template", "target_type", "required_markers"),
    [
        (
            "bootstrap_web.md",
            "Web",
            ("Source Map", "OpenAPI", "相似域名", "认证后资源", "动态 chunk"),
        ),
        (
            "bootstrap_miniapp.md",
            "小程序",
            (
                "status → 必要时 probe → 选择唯一 target_id → prepare",
                "Reqable",
                "混元",
                "CodeBuddy",
                "登录后 JS",
                "主包、分包、插件",
            ),
        ),
        (
            "bootstrap_app.md",
            "App",
            ("包名或 Bundle ID", "Deep Link", "设备上的其它 App", "认证后资源", "WebView JS"),
        ),
    ],
)
def test_bootstrap_target_profiles_only_add_platform_workflow(
    template: str,
    target_type: str,
    required_markers: tuple[str, ...],
) -> None:
    prompt = PromptRenderer().render(template, {})

    assert "主 `bootstrap.md` 为准" in prompt
    assert f"目标类型填写 `{target_type}`" in prompt
    assert "目标身份" in prompt
    assert "FINISH" not in prompt
    assert "asset_inventory.md" not in prompt
    assert "findings.md" not in prompt
    for marker in required_markers:
        assert marker in prompt


def test_scope_and_target_prompt_guidance_follow_effective_lists() -> None:
    assert _scope_guidance({}) == ""
    assert _scope_guidance({"enabled": False, "targets": ["legacy.example.com"]}) == ""
    assert "黑名单：blocked.example.com；其余网络目标允许" in _scope_guidance(
        {"enabled": True, "targets": [], "exclude": ["blocked.example.com"]}
    )
    assert "可以省略 --target" in _reason_target_guidance({})
    assert "不得从 Origin 推导或补造 Target" in _reason_target_guidance({})
    assert "必须至少提供一个 --target" in _reason_target_guidance(
        {"enabled": True, "targets": ["a.example.com"], "exclude": []}
    )
    assert _intent_targets_text([]) == "未指定独立 Target；按 Intent 描述和 Origin 初始信息执行"


def test_explore_prompt_prioritizes_injected_context_over_progressive_details() -> None:
    prompt = PromptRenderer().render(
        "explore.md",
        {
            "task_id": "t_001",
            "intent_id": "i002",
            "todo_id": "td001",
            "intent_description": "验证目标",
            "intent_targets": '["a.example.com"]',
            "origin": "a.example.com",
            "goal": "完成测试",
            "scope_guidance": _scope_guidance(
                {"enabled": True, "targets": ["a.example.com"], "exclude": []}
            ),
            "context_manifest_path": "/tmp/context.json",
            "run_dir": "/tmp/run",
            "skill_directories": "- /tmp/skills",
        },
    )

    assert "已由 AXIOM 注入" in prompt
    assert "按卡内路径按需读取" in prompt
    assert "严禁无脑全盘读取历史" in prompt


@pytest.mark.parametrize(
    "template",
    [
        "bootstrap.md",
        "reason.md",
        "reason_conclude.md",
        "explore.md",
        "explore_conclude.md",
    ],
)
def test_bash_capable_stage_prompts_require_whatisvul_before_stage_work(
    template: str,
) -> None:
    prompt = PromptRenderer().render(
        template,
        {
            "task_id": "t_001",
            "intent_id": "i002",
            "todo_id": "td001",
            "reason_run_id": "rr000001",
            "source_fact_ids": '["f002"]',
            "workspace_id": "rw_000001",
            "intent_description": "验证目标",
            "intent_targets": '["a.example.com"]',
            "origin": "a.example.com",
            "goal": "完成测试",
            "scope_guidance": _scope_guidance(
                {"enabled": True, "targets": ["a.example.com"], "exclude": []}
            ),
            "target_guidance": _reason_target_guidance(
                {"enabled": True, "targets": ["a.example.com"], "exclude": []}
            ),
            "context_manifest_path": "/tmp/context.json",
            "run_dir": "/tmp/run",
            "skill_directories": "- /tmp/skills/whatisvul",
        },
    )

    assert "axiom skill path whatisvul" in prompt
    assert "axiom skill doctor whatisvul" in prompt
    assert "whatisvul/SKILL.md" in prompt or "返回目录中的 `SKILL.md`" in prompt


def test_bootstrap_conclude_reuses_whatisvul_without_requesting_unavailable_bash() -> None:
    prompt = PromptRenderer().render(
        "bootstrap_conclude.md",
        {
            "task_id": "t_001",
            "intent_id": "i002",
            "run_dir": "/tmp/run",
        },
    )

    assert "收口阶段没有 Bash" in prompt
    assert "复用执行阶段已经读取的 `whatisvul/SKILL.md`" in prompt
    assert "axiom skill path whatisvul" not in prompt
    assert "axiom skill doctor whatisvul" not in prompt


@pytest.mark.parametrize("template", ["explore.md", "explore_conclude.md"])
def test_explore_prompts_show_plain_finish_without_markdown_wrapping(template: str) -> None:
    prompt = PromptRenderer().render(
        template,
        {
            "task_id": "t_001",
            "intent_id": "i002",
            "todo_id": "td001",
            "intent_description": "验证目标",
            "intent_targets": '["a.example.com"]',
            "origin": "a.example.com",
            "goal": "完成测试",
            "scope_guidance": _scope_guidance(
                {"enabled": True, "targets": ["a.example.com"], "exclude": []}
            ),
            "context_manifest_path": "/tmp/context.json",
            "run_dir": "/tmp/run",
            "skill_directories": "- /tmp/skills",
        },
    )

    assert "\nFINISH\n" in prompt
    assert "`FINISH`" not in prompt
    assert "不要描述检查结果或总结执行过程" in prompt


def test_explore_prompts_prioritize_core_outputs_over_optional_evidence() -> None:
    values = {
        "task_id": "t_001",
        "intent_id": "i002",
        "todo_id": "td001",
        "intent_description": "验证目标",
        "intent_targets": '["a.example.com"]',
        "origin": "a.example.com",
        "goal": "完成测试",
        "scope_guidance": _scope_guidance(
            {"enabled": True, "targets": ["a.example.com"], "exclude": []}
        ),
        "context_manifest_path": "/tmp/context.json",
        "run_dir": "/tmp/run",
        "skill_directories": "- /tmp/skills",
    }

    execute_prompt = PromptRenderer().render("explore.md", values)
    conclude_prompt = PromptRenderer().render("explore_conclude.md", values)

    assert "证据是审计材料，不是阶段完成条件" in execute_prompt
    assert "未上报漏洞的轮次**不写证据文件**" in execute_prompt
    assert "没有漏洞上报就不写证据文件" in execute_prompt
    assert "不要为了补齐证据重新运行 Nuclei、SQLMap、浏览器、爬虫" in execute_prompt
    assert "没做完没有关系" in conclude_prompt
    assert "不要为了补证据重新运行 Nuclei、SQLMap、浏览器、爬虫" in conclude_prompt
    assert "`evidence/` 完全可选" in conclude_prompt


def test_explore_prompts_expose_human_intervention_marker_contract() -> None:
    values = {
        "task_id": "t_001",
        "intent_id": "i002",
        "todo_id": "td001",
        "intent_description": "验证支付流程",
        "intent_targets": '[]',
        "origin": "agentpark 小程序",
        "goal": "完成测试",
        "scope_guidance": _scope_guidance({}),
        "context_manifest_path": "/tmp/context.json",
        "run_dir": "/tmp/run",
        "skill_directories": "- /tmp/skills",
    }

    execute_prompt = PromptRenderer().render("explore.md", values)
    conclude_prompt = PromptRenderer().render("explore_conclude.md", values)

    for prompt in (execute_prompt, conclude_prompt):
        assert "axiom explore human-intervention mark" in prompt
        assert "验证码" in prompt
        assert "真实设备" in prompt
        assert "工具失败" in prompt or "工具暂时失败" in prompt
        assert "leads.md" in prompt

    assert "真实支付" in execute_prompt
    assert "不会暂停任务" in execute_prompt
    assert "不会自动触发 Reason" in execute_prompt
    assert "不要因为工具暂时失败" in execute_prompt
    assert "原因不得包含凭证、验证码、支付信息或敏感响应正文" in execute_prompt
    assert "当前 Intent 仍必须正常生成两个产物并以 FINISH 收口" in execute_prompt


@pytest.mark.parametrize(
    ("execute_seconds", "expected_reminder", "expected_remaining"),
    [
        (600, 420.0, 180),
        (1200, 840.0, 360),
        (1800, 1260.0, 540),
    ],
)
def test_explore_soft_reminder_timing_is_dynamic(
    execute_seconds,
    expected_reminder,
    expected_remaining,
) -> None:
    assert _explore_soft_reminder_timing(execute_seconds) == (
        expected_reminder,
        expected_remaining,
    )


@pytest.mark.parametrize(
    ("execute_seconds", "expected_reminder"),
    [(600, 420.0), (1200, 840.0), (1800, 1260.0)],
)
def test_explore_uses_rpc_without_print_prompt_and_reports_soft_reminder(
    tmp_path,
    execute_seconds,
    expected_reminder,
) -> None:
    rpc = _RecordingRpcSupervisor(tmp_path)
    activities = []
    input_value = execution_input(tmp_path / "run").model_copy(
        update={
            "intent_kind": "explore",
            "intent_description": "验证对象级授权",
            "context_manifest_path": str(tmp_path / "context.json"),
            "execute_timeout_seconds": execute_seconds,
            "conclude_timeout_seconds": 180,
        }
    )
    Path(input_value.context_manifest_path).write_text("{}", encoding="utf-8")

    output = PiExploreExecutor(
        pi_worker(tmp_path / "unused-pi"),
        rpc_supervisor=rpc,
        activity_reporter=activities.append,
    ).execute(input_value, lease_active=lambda: True)

    assert output == "FINISH"
    assert len(rpc.specs) == 1
    spec = rpc.specs[0]
    assert spec.steer_after_seconds == expected_reminder
    assert spec.stream_idle_timeout_seconds == 120.0
    assert "上一轮模型响应流长时间没有完成" in (spec.stream_recovery_prompt or "")
    assert "不要重复已经完成的测试" in (spec.stream_recovery_prompt or "")
    assert f"剩余约 {execute_seconds * 3 // 10} 秒" in (spec.steer_message or "")
    assert "Evidence 有则保留，没有也不要补跑" in (spec.steer_message or "")
    assert spec.command[spec.command.index("--mode") + 1] == "rpc"
    assert "-p" not in spec.command
    assert activities[0].activity_kind == "soft_reminder"
    assert activities[0].activity_summary == "已发送预算软提醒，正在整理本轮产物"


def test_explore_conclude_also_uses_rpc_without_another_reminder(tmp_path) -> None:
    rpc = _RecordingRpcSupervisor(tmp_path, invoke_steer=False)
    input_value = execution_input(tmp_path / "run").model_copy(
        update={
            "intent_kind": "explore",
            "intent_description": "验证对象级授权",
            "context_manifest_path": str(tmp_path / "context.json"),
            "execute_timeout_seconds": 600,
            "conclude_timeout_seconds": 180,
            "mcp_tool_names": ["mcp__demo__selected__12345678"],
            "axiom_tool_keys": ["managed_probe"],
            "axiom_tool_extensions": [str(tmp_path / "managed_probe" / "index.ts")],
            "axiom_tool_snapshot_path": str(tmp_path / "axiom-tools.json"),
        }
    )
    Path(input_value.context_manifest_path).write_text("{}", encoding="utf-8")
    executor = PiExploreExecutor(
        pi_worker(tmp_path / "unused-pi"),
        rpc_supervisor=rpc,
    )
    executor._session_id = "session-test"

    output = executor.conclude(input_value, reason="execute_timeout", lease_active=lambda: True)

    assert output == "FINISH"
    assert len(rpc.specs) == 1
    spec = rpc.specs[0]
    assert spec.steer_after_seconds is None
    assert spec.steer_message is None
    assert spec.stream_idle_timeout_seconds is None
    assert spec.stream_recovery_prompt is None
    assert spec.command[spec.command.index("--mode") + 1] == "rpc"
    assert "-p" not in spec.command
    assert "mcp__demo__selected__12345678" in spec.command[spec.command.index("--tools") + 1]
    assert "axiom-mcp-tools" in " ".join(spec.command)
    assert "managed_probe/index.ts" not in " ".join(spec.command)
    assert "axiom-tool-activation" not in " ".join(spec.command)


def test_explore_execute_and_conclude_reuse_the_same_pi_session(tmp_path) -> None:
    rpc = _RecordingRpcSupervisor(tmp_path, invoke_steer=False)
    input_value = execution_input(tmp_path / "run").model_copy(
        update={
            "intent_kind": "explore",
            "intent_description": "验证对象级授权",
            "context_manifest_path": str(tmp_path / "context.json"),
            "execute_timeout_seconds": 600,
            "conclude_timeout_seconds": 180,
        }
    )
    Path(input_value.context_manifest_path).write_text("{}", encoding="utf-8")
    executor = PiExploreExecutor(
        pi_worker(tmp_path / "unused-pi"),
        rpc_supervisor=rpc,
    )

    executor.execute(input_value, lease_active=lambda: True)
    executor.conclude(input_value, reason="missing_finish", lease_active=lambda: True)

    assert len(rpc.specs) == 2
    session_ids = []
    for spec in rpc.specs:
        index = spec.command.index("--session-id")
        session_ids.append(spec.command[index + 1])
    assert session_ids[0] == session_ids[1]


def test_explore_browser_skill_loads_native_tools_and_reuses_one_browser_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AXIOM_BROWSER_STACK", "legacy")
    rpc = _RecordingRpcSupervisor(tmp_path, invoke_steer=False)
    browser_skill = tmp_path / "browser-control"
    browser_skill.mkdir(parents=True)
    (browser_skill / "SKILL.md").write_text(
        "---\nname: browser-control\n---\n", encoding="utf-8"
    )
    broker = _FakeBrowserBroker()
    input_value = execution_input(tmp_path / "run").model_copy(
        update={
            "intent_kind": "explore",
            "intent_description": "验证已登录页面",
            "skill_directories": [str(browser_skill)],
            "execute_timeout_seconds": 600,
            "conclude_timeout_seconds": 180,
        }
    )
    executor = PiExploreExecutor(
        pi_worker(tmp_path / "unused-pi"),
        rpc_supervisor=rpc,
        browser_broker=broker,
    )

    executor.execute(input_value, lease_active=lambda: True)
    executor.conclude(input_value, reason="missing_finish", lease_active=lambda: True)

    assert len(broker.registrations) == 1
    assert len(rpc.specs) == 2
    for spec in rpc.specs:
        allowed = spec.command[spec.command.index("--tools") + 1].split(",")
        assert set(BROWSER_TOOL_NAMES).issubset(allowed)
        assert "axiom-browser/index.ts" in " ".join(spec.command)
        assert spec.environment["AXIOM_BROWSER_SOCKET"] == "/tmp/browser-test.sock"
        assert spec.environment["AXIOM_BROWSER_TOKEN"] == "browser-token"

    executor.close()
    assert broker.closed == ["lease_test"]


def test_explore_browser_stack_skill_injects_wrapper_without_broker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rpc = _RecordingRpcSupervisor(tmp_path, invoke_steer=False)
    broker = _FakeBrowserBroker()
    input_value = execution_input(tmp_path / "run").model_copy(
        update={
            "intent_kind": "explore",
            "intent_description": "验证 BrowserSkill 栈注入",
            "execute_timeout_seconds": 600,
            "conclude_timeout_seconds": 180,
        }
    )
    executor = PiExploreExecutor(
        pi_worker(tmp_path / "unused-pi"),
        rpc_supervisor=rpc,
        browser_broker=broker,
    )

    executor.execute(input_value, lease_active=lambda: True)

    assert broker.registrations == []
    spec = rpc.specs[0]
    assert spec.environment["AXIOM_BSK"].endswith("axiom-bsk")
    assert any("browser-skill" in path for path in input_value.skill_directories)
    assert set(BROWSER_TOOL_NAMES).isdisjoint(spec.command)

    executor.close()
    assert broker.closed == []


def test_review_prompt_enforces_single_target_decision_and_review_artifact() -> None:
    values = {
        "task_id": "t_001",
        "review_run_id": "vr000001",
        "vulnerability_id": "v001",
        "review_reason": "审核新上报漏洞",
        "origin": "review.example.com",
        "goal": "验证漏洞",
        "scope_guidance": _scope_guidance(
            {"enabled": True, "targets": ["review.example.com"], "exclude": []}
        ),
        "context_manifest_path": "/tmp/context/manifest.json",
        "run_dir": "/tmp/run",
        "skill_directories": "- /tmp/skills/vuln-review",
    }

    prompt = PromptRenderer().render("review.md", values)
    conclude = PromptRenderer().render("review_conclude.md", values)

    assert "只审核当前一个漏洞" in prompt
    assert "不创建 Fact、Todo 或 Intent" in prompt
    assert "review_target.json" in prompt
    assert "axiom skill path vuln-review" in prompt
    assert "按该 Skill 的“必读附件”规则读取对应状态示例" in prompt
    assert "未分配该 Skill 时不要把它当作缺失依赖" in prompt
    assert "vuln_report-" in prompt
    assert "axiom review decision submit verified" in prompt
    assert "needs-human" in prompt
    assert "默认必须进行至少一次新的独立验证" in prompt
    assert "不得只复述文件内容后直接裁决" in prompt
    assert "至少一个关键对照结果" in prompt
    assert "不再要求 CVSS 向量" in prompt
    assert "精炼、专业、直击裁决要害" in prompt
    assert "只允许能证明漏洞存在的关键证据" in prompt
    assert "最后一条 assistant 消息必须只包含 6 个 ASCII 字符" in prompt
    assert "不修改漏洞原始正文" in conclude
    assert "不得仅根据旧 Finding/Evidence 强行提交" in conclude
    assert "references/*-example.md" in conclude
    assert "vuln_report-" in conclude
    assert "axiom review status" in conclude


def test_review_execute_and_conclude_use_json_mode_and_reuse_session(tmp_path) -> None:
    argument_log = tmp_path / "review-argv.txt"
    script = make_script(
        tmp_path,
        "printf '%s\\n' \"$@\" >> \"$ARG_LOG\"\n"
        "printf '%s\\n' --CALL-END-- >> \"$ARG_LOG\"\n"
        "printf 'FINISH'",
    )
    worker = pi_worker(script).model_copy(
        update={
            "capabilities": ["review"],
            "config": {
                "binary": str(script),
                "model": "test-model",
                "environment": {"ARG_LOG": str(argument_log)},
            },
        }
    )
    run_dir = tmp_path / "review-run"
    run_dir.mkdir()
    context = run_dir / "manifest.json"
    context.write_text("{}", encoding="utf-8")
    input_value = ExecutionInput(
        task_id="t_001",
        project_id="p_001",
        review_run_id="vr000001",
        vulnerability_id="v001",
        lease_id="lease-review",
        worker_name="worker-a",
        intent_kind="review",
        intent_description="审核待验证漏洞",
        origin="review.example.com",
        goal="验证漏洞",
        scope={"enabled": True, "targets": ["review.example.com"]},
        run_dir=str(run_dir),
        execute_timeout_seconds=10,
        conclude_timeout_seconds=10,
        context_manifest_path=str(context),
    )
    executor = PiReviewExecutor(worker)

    assert executor.execute(input_value, lease_active=lambda: True) == "FINISH"
    assert (
        executor.conclude(
            input_value,
            reason="missing_review_decision",
            lease_active=lambda: True,
        )
        == "FINISH"
    )

    calls = argument_log.read_text(encoding="utf-8").split("--CALL-END--\n")
    calls = [item.splitlines() for item in calls if item.strip()]
    assert len(calls) == 2
    session_ids = [call[call.index("--session-id") + 1] for call in calls]
    assert session_ids[0] == session_ids[1]
    assert all("-p" in call for call in calls)
    assert all(call[call.index("--mode") + 1] == "json" for call in calls)
    assert all(
        call[call.index("--tools") + 1] == "read,write,edit,bash"
        for call in calls
    )


class _FakeBrowserBroker:
    def __init__(self):
        self.registrations = []
        self.closed = []

    def register_lease(self, **kwargs):
        self.registrations.append(kwargs)
        return BrowserLeaseHandle(
            socket_path="/tmp/browser-test.sock",
            token="browser-token",
            lease_id=kwargs["lease_id"],
        )

    def close_lease(self, handle):
        self.closed.append(handle.lease_id)


class _RecordingRpcSupervisor:
    def __init__(self, tmp_path: Path, *, invoke_steer: bool = True):
        self.tmp_path = tmp_path
        self.invoke_steer = invoke_steer
        self.specs = []

    def run(self, spec, *, should_cancel=None, on_steer=None):
        self.specs.append(spec)
        assert should_cancel is None or should_cancel() is None
        if self.invoke_steer and spec.steer_after_seconds is not None and on_steer is not None:
            on_steer()
        log_dir = self.tmp_path / "fake-rpc"
        log_dir.mkdir(exist_ok=True)
        stdout_path = log_dir / "stdout.log"
        stderr_path = log_dir / "stderr.log"
        stdout_path.write_text("", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        return ProcessResult(
            returncode=0,
            stdout="",
            stderr="",
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            last_assistant_text="FINISH",
        )
