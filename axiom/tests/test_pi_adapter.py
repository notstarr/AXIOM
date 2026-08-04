from __future__ import annotations

import json
from pathlib import Path

import pytest

from axiom.dispatcher.bootstrap import BootstrapExecutionTimeout, TransientWorkerError
from axiom.dispatcher.pi import (
    CONTEXT_ATTENTION_SYSTEM_PROMPT,
    NO_BASH_CONTEXT_ATTENTION_SYSTEM_PROMPT,
    PiBootstrapExecutor,
    PiExploreExecutor,
    PromptRenderer,
    _explore_soft_reminder_timing,
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


def test_pi_adapter_closes_browser_tabs_registered_to_finished_lease(
    tmp_path: Path,
) -> None:
    browser_skill = tmp_path / "browser-control"
    cleanup_script = browser_skill / "scripts" / "cdp_ctl.py"
    cleanup_script.parent.mkdir(parents=True)
    cleanup_script.write_text(
        "import json, os, pathlib, sys\n"
        "pathlib.Path(os.environ['CLEANUP_RECORD']).write_text("
        "json.dumps(sys.argv), encoding='utf-8')\n",
        encoding="utf-8",
    )
    record = tmp_path / "cleanup.json"
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
                "environment": {"CLEANUP_RECORD": str(record)},
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
    argv = json.loads(record.read_text(encoding="utf-8"))
    assert argv[-2:] == ["close-lease-tabs", "lease_test"]


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
    assert native_tool_names("reason", "execute") == ["read", "write", "edit", "bash"]
    assert native_tool_names("reason", "conclude") == ["read", "write", "edit", "bash"]
    assert native_tool_names("explore", "execute") == ["read", "write", "edit", "bash"]
    assert native_tool_names("explore", "conclude") == ["read", "write", "edit", "bash"]


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
            "context_manifest_path": "/tmp/context.json",
            "run_dir": "/tmp/run",
            "skill_directories": "- /tmp/skills",
        },
    )

    assert "一个待办只描述一个攻击主题" in prompt
    assert "最多包含 2–3 个紧密相关" in prompt
    assert "获取对象 ID、准备登录态" in prompt
    assert "完整读取其中的 `hints.json`、`knowledge.json` 和 `vulnerabilities.json`" in prompt
    assert "Reason 上下文不会披露历史 Goal、Goal Evaluation 及其证据" in prompt
    assert "goal_cycles.json" not in prompt
    assert "goal_evaluations.json" not in prompt
    assert "status_reason" in prompt
    assert "本轮新事实（Fact）：`[\"f002\", \"f003\"]`" in prompt
    assert "完整读取上下文清单中的 `todos.json` 索引" in prompt
    assert "完成新事实（若有）和 `todo_review_ids` 处理前，禁止新增待办或派发执行意图" in prompt
    assert "已有同义开放执行意图时禁止重复派发" in prompt
    assert "`0` 个执行意图不是默认结果" in prompt
    assert "必须通过以下收口门槛的例外结果" in prompt
    assert "若无法证明已穷尽" in prompt
    assert "至少派发一个具体的非重复 Intent" in prompt
    assert "--execute-seconds 600 --conclude-seconds 120" not in prompt


def test_reason_conclude_prompt_allows_only_a_strictly_justified_no_op_plan() -> None:
    prompt = PromptRenderer().render(
        "reason_conclude.md",
        {
            "task_id": "t_001",
            "reason_run_id": "rr000001",
            "workspace_id": "rw_000001",
            "run_dir": "/tmp/run",
        },
    )

    assert "`0` 个执行意图不是默认结果" in prompt
    assert "必须通过严格门槛" in prompt
    assert "提交 0 Intent 时必须记录逐项检查结果和阻塞证据" in prompt
    assert "没有新 Fact" in prompt
    assert "至少保留一个可执行" not in prompt


def test_bootstrap_prompt_receives_manifest_and_requires_priority_context_reading() -> None:
    prompt = PromptRenderer().render(
        "bootstrap.md",
        {
            "task_id": "t_001",
            "intent_id": "i001",
            "origin": "a.example.com",
            "goal": "完成测试",
            "context_manifest_path": "/tmp/context.json",
            "run_dir": "/tmp/run",
            "skill_directories": "- /tmp/skills",
        },
    )

    assert "上下文清单（Context Manifest）：`/tmp/context.json`" in prompt
    assert "完整读取其中的 `hints.json`、`knowledge.json` 和 `vulnerabilities.json`" in prompt
    assert "避免重复测试和重复上报" in prompt


def test_explore_prompt_requires_priority_context_before_progressive_details() -> None:
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
            "context_manifest_path": "/tmp/context.json",
            "run_dir": "/tmp/run",
            "skill_directories": "- /tmp/skills",
        },
    )

    assert "完整读取其中的 `hints.json`、`knowledge.json` 和 `vulnerabilities.json`" in prompt
    assert "status_reason" in prompt
    assert "再按需读取相关事实、执行意图、待办、产物、证据" in prompt


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
            "context_manifest_path": "/tmp/context.json",
            "run_dir": "/tmp/run",
            "skill_directories": "- /tmp/skills/whatisvul",
        },
    )

    assert "axiom skill path whatisvul" in prompt
    assert "axiom skill doctor whatisvul" in prompt
    assert "完整读取" in prompt
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
        "context_manifest_path": "/tmp/context.json",
        "run_dir": "/tmp/run",
        "skill_directories": "- /tmp/skills",
    }

    execute_prompt = PromptRenderer().render("explore.md", values)
    conclude_prompt = PromptRenderer().render("explore_conclude.md", values)

    assert "证据是可选审计材料，不是阶段完成条件" in execute_prompt
    assert "没有可用证据时直接省略" in execute_prompt
    assert "补齐证据" in execute_prompt
    assert "没做完没有关系" in conclude_prompt
    assert "不要为了补证据重新运行 Nuclei、SQLMap、浏览器、爬虫" in conclude_prompt
    assert "`evidence/` 完全可选" in conclude_prompt


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
