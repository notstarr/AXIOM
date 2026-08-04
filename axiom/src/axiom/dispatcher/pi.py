from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from axiom.dispatcher.bootstrap import (
    AxiomToolLoadError,
    BootstrapExecutionTimeout,
    TransientWorkerError,
)
from axiom.dispatcher.activity import SessionActivityTracker
from axiom.dispatcher.heartbeat import LeaseLostError
from axiom.dispatcher.explore import ExploreExecutionTimeout
from axiom.dispatcher.process import (
    ProcessResult,
    ProcessSpec,
    ProcessSupervisor,
    RpcProcessSpec,
    RpcProcessSupervisor,
)
from axiom.dispatcher.reason import ReasonExecutionTimeout
from axiom.dispatcher.tool_path import (
    build_tool_path,
    discover_tool_environment,
    prepare_runtime_bin,
)
from axiom.modules.execution.schemas import ActivityCommand, ExecutionInput
from axiom.modules.phase_capabilities.builtins import native_tool_names
from axiom.modules.reason.schemas import ReasonWorkspace
from axiom.modules.token_usage.schemas import TokenUsageBatchCommand
from axiom.modules.workers.pi_config import PiWorkerConfig
from axiom.modules.workers.schemas import Worker


PiSettings = PiWorkerConfig
EXPLORE_SOFT_REMINDER_RATIO = 0.70
CONTEXT_ATTENTION_SYSTEM_PROMPT = (
    "在开始规划或执行任何操作前，必须先读取 Context Manifest，并完整读取其中的 "
    "hints.json、knowledge.json、vulnerabilities.json；随后查看本轮相关 Skill，再按需展开 "
    "Fact、Intent、Todo、Artifact、Evidence 和 Goal Evaluation。Task Hint 是任务级约束，"
    "Project Knowledge 是业务背景，Skill 是执行方法，已有漏洞及 status_reason 必须用于避免"
    "重复规划、测试和上报。冲突时遵循：系统规则/Scope/阶段合同 > Task Hint > Project "
    "Knowledge > Skill 方法论 > 已有漏洞及状态原因 > 普通 Context 与工具输出。"
    "whatisvul 是 Bootstrap、Reason、Explore 全阶段的强制前置 Skill，不属于按需读取项："
    "读取高价值 Context 后、进行任何探测、规划、漏洞判断或产物整理前，必须运行 "
    "axiom skill path whatisvul 并完整读取返回目录中的 SKILL.md。读取失败时先运行 "
    "axiom skill doctor whatisvul 排障并重试；不得用记忆、常识或其它 Skill 替代。"
)
NO_BASH_CONTEXT_ATTENTION_SYSTEM_PROMPT = (
    "在开始整理任何产物前，必须先读取 Context Manifest，并完整读取其中的 hints.json、"
    "knowledge.json、vulnerabilities.json；再按需展开 Fact、Intent、Todo、Artifact、Evidence "
    "和 Goal Evaluation。Task Hint 是任务级约束，Project Knowledge 是业务背景，Skill 是执行"
    "方法，已有漏洞及 status_reason 必须用于避免重复判断。冲突时遵循：系统规则/Scope/阶段"
    "合同 > Task Hint > Project Knowledge > Skill 方法论 > 已有漏洞及状态原因 > 普通 Context "
    "与工具输出。本收口没有 Bash，不得调用 axiom skill path/doctor；必须复用当前会话执行阶段"
    "已经读取的 whatisvul/SKILL.md。若执行阶段未能读取，只能如实整理已有材料，不能凭记忆补造"
    "漏洞标准或重新探测。"
)


class PromptRenderer:
    def __init__(self, root: Path | None = None):
        self.root = root or Path(__file__).with_name("prompts")

    def render(self, name: str, values: dict[str, str]) -> str:
        template = (self.root / name).read_text(encoding="utf-8")
        for key, value in values.items():
            template = template.replace("{" + key + "}", value)
        return template


class PiBootstrapExecutor:
    """Pi adapter with bounded process output and file-first Bootstrap results."""

    def __init__(
        self,
        worker: Worker,
        *,
        supervisor: ProcessSupervisor | None = None,
        rpc_supervisor: RpcProcessSupervisor | None = None,
        prompt_renderer: PromptRenderer | None = None,
        default_conclude_seconds: int = 180,
        activity_reporter: Callable[[ActivityCommand], None] | None = None,
        token_usage_reporter: Callable[[TokenUsageBatchCommand], None] | None = None,
        api_url: str = "http://127.0.0.1:8899",
        service_token: str = "",
        intervention_poller: Callable[[], list[tuple[str, str]]] | None = None,
        intervention_result_reporter: Callable[[str, bool, str], None] | None = None,
    ):
        self.worker = worker
        self.settings = PiSettings.model_validate(worker.config)
        self.supervisor = supervisor or ProcessSupervisor()
        self.rpc_supervisor = rpc_supervisor or RpcProcessSupervisor()
        self.prompts = prompt_renderer or PromptRenderer()
        self.default_conclude_seconds = default_conclude_seconds
        self.activity_reporter = activity_reporter
        self.token_usage_reporter = token_usage_reporter
        self.api_url = api_url.rstrip("/")
        self.service_token = service_token
        self.intervention_poller = intervention_poller
        self.intervention_result_reporter = intervention_result_reporter
        self._session_id: str | None = None

    def execute(self, execution_input: ExecutionInput, *, lease_active) -> object:
        if not execution_input.context_manifest_path:
            raise TransientWorkerError("Bootstrap Context manifest is unavailable")
        self._session_id = self._session_id or uuid.uuid4().hex
        result = self._run(
            execution_input,
            self.prompts.render(
                "bootstrap.md",
                {
                    "task_id": execution_input.task_id,
                    "intent_id": execution_input.intent_id,
                    "origin": execution_input.origin,
                    "goal": execution_input.goal,
                    "context_manifest_path": execution_input.context_manifest_path,
                    "run_dir": execution_input.run_dir,
                    "skill_directories": _format_skill_directories(execution_input.skill_directories),
                },
            ),
            lease_active=lease_active,
            timeout_seconds=execution_input.execute_timeout_seconds or 900,
            tool_names=native_tool_names("bootstrap", "execute"),
        )
        if result.cancelled:
            raise LeaseLostError(result.cancel_reason or "worker cancelled")
        if result.timed_out:
            raise BootstrapExecutionTimeout("Bootstrap execute budget exhausted")
        self._raise_on_process_failure(result)
        return self._last_assistant_text(result)

    def conclude(self, execution_input: ExecutionInput, *, reason: str, lease_active) -> object:
        if not self._session_id:
            raise TransientWorkerError("Bootstrap session is unavailable for conclude")
        result = self._run(
            execution_input,
            self.prompts.render(
                "bootstrap_conclude.md",
                {
                    "task_id": execution_input.task_id,
                    "intent_id": execution_input.intent_id,
                    "run_dir": execution_input.run_dir,
                },
            )
            + f"\n收口原因：{reason}\n",
            lease_active=lease_active,
            timeout_seconds=execution_input.conclude_timeout_seconds or self.default_conclude_seconds,
            tool_names=native_tool_names("bootstrap", "conclude"),
            enable_axiom_tools=False,
        )
        if result.cancelled:
            raise LeaseLostError(result.cancel_reason or "worker cancelled")
        if result.timed_out:
            raise BootstrapExecutionTimeout("Bootstrap conclude budget exhausted")
        self._raise_on_process_failure(result)
        return self._last_assistant_text(result)

    def _run(
        self,
        execution_input: ExecutionInput,
        prompt: str,
        *,
        lease_active,
        timeout_seconds: int,
        tool_names: list[str],
        extra_environment: dict[str, str] | None = None,
        rpc: bool = False,
        steer_after_seconds: float | None = None,
        steer_message: str | None = None,
        enable_axiom_tools: bool = True,
    ) -> ProcessResult:
        session_dir = Path(self.settings.session_dir or Path(execution_input.run_dir) / "sessions")
        session_dir.mkdir(parents=True, exist_ok=True)
        agent_dir = session_dir.parent
        self._write_models_file(agent_dir)
        environment = dict(self.settings.environment)
        environment.update(
            {
                "AXIOM_RUN_DIR": execution_input.run_dir,
                "AXIOM_TASK_ID": execution_input.task_id,
                "AXIOM_PROJECT_ID": execution_input.project_id,
                "AXIOM_LEASE_ID": execution_input.lease_id,
                "AXIOM_PHASE": execution_input.intent_kind,
                "AXIOM_WORKER": execution_input.worker_name,
                "AXIOM_GUARD_ENABLED": "1" if execution_input.guard_enabled else "0",
                "PI_CODING_AGENT_DIR": str(agent_dir),
                "AXIOM_SKILL_DIRECTORIES": ":".join(execution_input.skill_directories),
                "AXIOM_SERVER": self.api_url,
                "AXIOM_SCOPE_JSON": json.dumps(
                    execution_input.scope,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                "AXIOM_TOOL_KEYS": json.dumps(
                    execution_input.axiom_tool_keys,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                "AXIOM_TOOL_SNAPSHOT_PATH": execution_input.axiom_tool_snapshot_path or "",
                "AXIOM_CAPABILITY_SNAPSHOT_PATH": execution_input.capability_snapshot_path or "",
                "AXIOM_MCP_TOOL_NAMES": json.dumps(
                    execution_input.mcp_tool_names,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                "AXIOM_PYTHON": sys.executable,
            }
        )
        # Stage capability bindings are the runtime authority.  Apply their
        # environment after the persisted model-backend config so stale legacy
        # Worker MCP settings cannot leak tools into a new Lease.
        environment.update(execution_input.capability_environment)
        if execution_input.intent_id:
            environment["AXIOM_INTENT_ID"] = execution_input.intent_id
        if execution_input.reason_run_id:
            environment["AXIOM_REASON_RUN_ID"] = execution_input.reason_run_id
        if self.service_token:
            environment["AXIOM_SERVICE_TOKEN"] = self.service_token
        if extra_environment:
            environment.update(extra_environment)
        if self.settings.base_url:
            environment["PI_BASE_URL"] = self.settings.base_url
        if self.settings.provider_api:
            environment["PI_PROVIDER_API"] = self.settings.provider_api
        if self.settings.api_key:
            environment["PI_API_KEY"] = self.settings.api_key
        environment["PI_MODEL_CONTEXT_WINDOW"] = str(self.settings.context_window)
        venv_bin = Path(sys.executable).parent
        runtime_bin = prepare_runtime_bin(
            agent_dir / ".axiom-bin",
            python_executable=sys.executable,
            axiom_executable=venv_bin / "axiom",
        )
        environment["PATH"] = build_tool_path(
            inherited=environment.get("PATH", os.environ.get("PATH", "")),
            configured=environment.get("AXIOM_TOOL_PATH"),
            runtime_bin=runtime_bin,
            fallback_bins=[venv_bin],
        )
        discovered_environment = discover_tool_environment(
            {**os.environ, **environment}
        )
        for name, value in discovered_environment.items():
            environment.setdefault(name, value)
        argv = self._build_argv(
            prompt,
            session_dir,
            tool_names,
            skill_paths=execution_input.skill_directories,
            guard_enabled=execution_input.guard_enabled,
            mode="rpc" if rpc else "json",
            axiom_tool_extensions=(
                execution_input.axiom_tool_extensions if enable_axiom_tools else []
            ),
            axiom_tool_keys=(
                execution_input.axiom_tool_keys if enable_axiom_tools else []
            ),
            mcp_tool_names=execution_input.mcp_tool_names,
        )
        tracker = None
        if self.activity_reporter is not None or self.token_usage_reporter is not None:
            tracker = SessionActivityTracker(
                session_dir,
                execution_input.lease_id,
                self.activity_reporter,
                usage_reporter=self.token_usage_reporter,
                session_id=self._session_id,
                process_dir=Path(execution_input.run_dir) / "process",
            )
        if tracker is not None:
            tracker.start()
        try:
            def should_cancel() -> str | None:
                if (tracker is not None and tracker.lease_lost) or not lease_active():
                    return "lease_lost"
                return None

            if rpc:
                result = self.rpc_supervisor.run(
                    RpcProcessSpec(
                        command=argv,
                        log_dir=Path(execution_input.run_dir) / "process",
                        timeout_seconds=timeout_seconds,
                        environment=environment,
                        prompt=prompt,
                        steer_after_seconds=steer_after_seconds,
                        steer_message=steer_message,
                        intervention_poller=self.intervention_poller if execution_input.intent_kind == "explore" else None,
                        intervention_result_reporter=self.intervention_result_reporter if execution_input.intent_kind == "explore" else None,
                    ),
                    should_cancel=should_cancel,
                    on_steer=lambda: self._report_soft_reminder(execution_input),
                )
            else:
                result = self.supervisor.run(
                    ProcessSpec(
                        command=argv,
                        log_dir=Path(execution_input.run_dir) / "process",
                        timeout_seconds=timeout_seconds,
                        environment=environment,
                    ),
                    should_cancel=should_cancel,
                )
        finally:
            if tracker is not None:
                tracker.stop()
            self._cleanup_browser_tabs(execution_input, environment)
        if tracker is not None and tracker.lease_lost:
            raise LeaseLostError("Intent activity lease is no longer active")
        return result

    @staticmethod
    def _cleanup_browser_tabs(
        execution_input: ExecutionInput, environment: dict[str, str]
    ) -> None:
        """Best-effort cleanup for tabs created by this Worker Lease.

        Browser tabs live outside the Pi process group.  Without an explicit
        Lease cleanup they accumulate across retries and eventually make
        Playwright's CDP attachment unusable.  The browser script only closes
        tabs registered by ``cdp_ctl.py`` for this exact Lease; human tabs and
        tabs owned by concurrent Workers are never selected.
        """

        if environment.get("AXIOM_BROWSER_PRESERVE_LEASE_TABS") == "1":
            return
        script = next(
            (
                Path(directory) / "scripts" / "cdp_ctl.py"
                for directory in execution_input.skill_directories
                if Path(directory).name == "browser-control"
                and (Path(directory) / "scripts" / "cdp_ctl.py").is_file()
            ),
            None,
        )
        if script is None:
            return
        command = [
            sys.executable,
            str(script),
            "--cdp",
            environment.get("CDP_URL", "http://localhost:9223"),
            "close-lease-tabs",
            execution_input.lease_id,
        ]
        try:
            subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=8,
                check=False,
                env={**os.environ, **environment},
            )
        except (OSError, subprocess.TimeoutExpired):
            pass

    def _build_argv(
        self,
        prompt: str,
        session_dir: Path,
        tool_names: list[str],
        *,
        skill_paths: list[str] | None = None,
        guard_enabled: bool = False,
        mode: str = "json",
        axiom_tool_extensions: list[str] | None = None,
        axiom_tool_keys: list[str] | None = None,
        mcp_tool_names: list[str] | None = None,
    ) -> list[str]:
        argv = [
            self.settings.binary,
            "--provider",
            self.settings.provider,
            "--model",
            self.settings.model,
            "--session-dir",
            str(session_dir),
            "--mode",
            mode,
            "--append-system-prompt",
            (
                CONTEXT_ATTENTION_SYSTEM_PROMPT
                if "bash" in tool_names
                else NO_BASH_CONTEXT_ATTENTION_SYSTEM_PROMPT
            ),
        ]
        if self._session_id:
            argv.extend([self.settings.session_flag, self._session_id])
        # Always make the level explicit.  Pi persists thinking changes in a
        # session, so omitting the flag is weaker than explicitly selecting
        # ``off`` for a Worker whose thinking switch is disabled.
        argv.extend(["--thinking", self.settings.effective_thinking_level])
        # Pi treats ``--tools`` as an allowlist for *all* tools, including
        # extension-registered tools.  Passing only the four native names
        # would therefore hide a correctly loaded AXIOM Tool before the
        # activation extension gets a chance to call ``setActiveTools``.
        # Keep the stage's native allowlist and add only this Lease's AXIOM
        # keys; unrelated AXIOM Tools remain neither loaded nor exposed.
        selected_keys = list(dict.fromkeys(axiom_tool_keys or []))
        selected_mcp_tools = list(dict.fromkeys(mcp_tool_names or []))
        allowed_tools = list(
            dict.fromkeys([*tool_names, *selected_mcp_tools, *selected_keys])
        )
        if allowed_tools:
            argv.extend(["--tools", ",".join(allowed_tools)])
        # Skill exposure is stage-scoped.  ``settings.skill_paths`` is a
        # legacy backend field and is intentionally not used for new Leases.
        for path in dict.fromkeys(skill_paths or []):
            argv.extend(["--skill", path])
        if guard_enabled:
            extension = Path(__file__).with_name("extensions") / "axiom-guard" / "index.ts"
            argv.extend(["--extension", str(extension)])
        if selected_mcp_tools:
            extension = (
                Path(__file__).with_name("extensions")
                / "axiom-mcp-tools"
                / "index.ts"
            )
            argv.extend(["--extension", str(extension)])
        selected_extensions = list(dict.fromkeys(axiom_tool_extensions or []))
        for extension in selected_extensions:
            argv.extend(["--extension", extension])
        if selected_keys:
            activation = (
                Path(__file__).with_name("extensions")
                / "axiom-tool-activation"
                / "index.ts"
            )
            argv.extend(["--extension", str(activation)])
        if mode != "rpc":
            argv.extend(["-p", prompt])
        return argv

    def _report_soft_reminder(self, execution_input: ExecutionInput) -> None:
        if self.activity_reporter is None:
            return
        self.activity_reporter(
            ActivityCommand(
                lease_id=execution_input.lease_id,
                activity_kind="soft_reminder",
                activity_summary="已发送预算软提醒，正在整理本轮产物",
                activity_tool="",
            )
        )

    def _write_models_file(self, agent_dir: Path) -> None:
        """Materialize Pi's custom provider registry for this isolated run.

        ``PI_BASE_URL`` is not a Pi provider registration mechanism.  Pi reads
        custom OpenAI-compatible providers from ``models.json`` under
        ``PI_CODING_AGENT_DIR``; without this file ``--provider axiom`` only
        sees whatever happens to be configured in the user's global Pi home.
        """
        if not self.settings.base_url or not self.settings.model:
            return
        model: dict[str, Any] = {
            "id": self.settings.model,
            "name": self.settings.model,
            "contextWindow": self.settings.context_window,
            # Pi only honors --thinking when the selected model advertises
            # reasoning support.  Without this field it clamps every requested
            # level to off before the first provider request.
            "reasoning": self.settings.thinking_enabled,
        }
        if self.settings.thinking_enabled and self.settings.thinking_level in {
            "xhigh",
            "max",
        }:
            # Pi 0.82.1 intentionally requires explicit model opt-in for the
            # two levels above high.  Selecting one in AXIOM is that opt-in;
            # an upstream provider may still reject a level it does not expose.
            model["thinkingLevelMap"] = {
                self.settings.thinking_level: self.settings.thinking_level
            }
        provider: dict[str, Any] = {
            "baseUrl": self.settings.base_url,
            "api": self.settings.provider_api,
            "apiKey": self.settings.api_key or "none",
            "models": [model],
        }
        payload = {"providers": {self.settings.provider: provider}}
        agent_dir.mkdir(parents=True, exist_ok=True)
        path = agent_dir / "models.json"
        temporary = agent_dir / ".models.json.tmp"
        temporary.write_text(
            json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        temporary.replace(path)

    @staticmethod
    def _raise_on_process_failure(result: ProcessResult) -> None:
        # Pi currently reports extension event failures on stderr even when
        # the outer CLI exits with status 0.  AXIOM Tool activation is a
        # pre-model contract, so never let that marker degrade into a later
        # FINISH/artifact error merely because Pi normalized its exit code.
        combined_detail = "\n".join(
            value.strip() for value in (result.stderr, result.stdout) if value.strip()
        )
        if "axiom_tool_load_failed" in combined_detail:
            raise AxiomToolLoadError(combined_detail[-2_000:])
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "Pi exited unsuccessfully"
            raise TransientWorkerError(detail[-2_000:])

    @staticmethod
    def _last_assistant_text(result: ProcessResult) -> str:
        if result.last_assistant_text is not None:
            return result.last_assistant_text
        assistant_messages: list[str] = []
        text_events: list[str] = []
        for line in result.stdout.splitlines():
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            message = payload.get("message")
            if isinstance(message, dict) and message.get("role") == "assistant":
                text = "\n".join(_message_text(message)).strip()
                if text:
                    assistant_messages.append(text)
            if payload.get("type") == "text_end" and isinstance(payload.get("content"), str):
                text = payload["content"].strip()
                if text:
                    text_events.append(text)
        if assistant_messages:
            return assistant_messages[-1]
        if text_events:
            return text_events[-1]
        return result.stdout.strip()


class PiReasonExecutor(PiBootstrapExecutor):
    """Incremental Reason adapter backed by the HTTP-only Axiom CLI."""

    def execute(
        self,
        execution_input: ExecutionInput,
        *,
        workspace: ReasonWorkspace,
        lease_active,
    ) -> str:
        if not execution_input.context_manifest_path:
            raise TransientWorkerError("Reason Context manifest is unavailable")
        self._session_id = self._session_id or uuid.uuid4().hex
        result = self._run(
            execution_input,
            self.prompts.render(
                "reason.md",
                {
                    "task_id": execution_input.task_id,
                    "reason_run_id": execution_input.reason_run_id or "",
                    "intent_description": execution_input.intent_description,
                    "source_fact_ids": json.dumps(
                        execution_input.intent_source_facts,
                        ensure_ascii=False,
                    ),
                    "origin": execution_input.origin,
                    "goal": execution_input.goal,
                    "context_manifest_path": execution_input.context_manifest_path,
                    "run_dir": execution_input.run_dir,
                    "skill_directories": _format_skill_directories(execution_input.skill_directories),
                },
            ),
            lease_active=lease_active,
            timeout_seconds=execution_input.execute_timeout_seconds or 600,
            tool_names=native_tool_names("reason", "execute"),
            extra_environment={"AXIOM_REASON_WORKSPACE_ID": workspace.id},
        )
        if result.cancelled:
            raise LeaseLostError(result.cancel_reason or "worker cancelled")
        if result.timed_out:
            raise ReasonExecutionTimeout("Reason execution budget exhausted")
        self._raise_on_process_failure(result)
        return self._last_assistant_text(result)

    def conclude(
        self,
        execution_input: ExecutionInput,
        *,
        workspace: ReasonWorkspace,
        reason: str,
        lease_active,
    ) -> str:
        if not self._session_id:
            raise TransientWorkerError("Reason session is unavailable for conclude")
        result = self._run(
            execution_input,
            self.prompts.render(
                "reason_conclude.md",
                {
                    "task_id": execution_input.task_id,
                    "reason_run_id": execution_input.reason_run_id or "",
                    "workspace_id": workspace.id,
                    "run_dir": execution_input.run_dir,
                },
            )
            + f"\n收口原因：{reason}\n",
            lease_active=lease_active,
            timeout_seconds=execution_input.conclude_timeout_seconds
            or self.default_conclude_seconds,
            tool_names=native_tool_names("reason", "conclude"),
            extra_environment={"AXIOM_REASON_WORKSPACE_ID": workspace.id},
            enable_axiom_tools=False,
        )
        if result.cancelled:
            raise LeaseLostError(result.cancel_reason or "worker cancelled")
        if result.timed_out:
            raise ReasonExecutionTimeout("Reason conclude budget exhausted")
        self._raise_on_process_failure(result)
        return self._last_assistant_text(result)


class PiExploreExecutor(PiBootstrapExecutor):
    """Pi Explore adapter with one execute and same-session bounded closeout."""

    def execute(self, execution_input: ExecutionInput, *, lease_active) -> object:
        if not execution_input.context_manifest_path:
            raise TransientWorkerError("Explore Context manifest is unavailable")
        self._session_id = self._session_id or uuid.uuid4().hex
        timeout_seconds = execution_input.execute_timeout_seconds or 900
        steer_after_seconds, remaining_seconds = _explore_soft_reminder_timing(timeout_seconds)
        result = self._run(
            execution_input,
            self.prompts.render(
                "explore.md",
                {
                    "task_id": execution_input.task_id,
                    "intent_id": execution_input.intent_id,
                    "todo_id": execution_input.todo_id or "无",
                    "intent_description": execution_input.intent_description,
                    "intent_targets": json.dumps(
                        execution_input.intent_targets,
                        ensure_ascii=False,
                    ),
                    "origin": execution_input.origin,
                    "goal": execution_input.goal,
                    "context_manifest_path": execution_input.context_manifest_path,
                    "run_dir": execution_input.run_dir,
                    "skill_directories": _format_skill_directories(execution_input.skill_directories),
                },
            ),
            lease_active=lease_active,
            timeout_seconds=timeout_seconds,
            tool_names=native_tool_names("explore", "execute"),
            rpc=True,
            steer_after_seconds=steer_after_seconds,
            steer_message=_explore_soft_reminder(remaining_seconds),
        )
        if result.cancelled:
            raise LeaseLostError(result.cancel_reason or "worker cancelled")
        if result.timed_out:
            raise ExploreExecutionTimeout("Explore execute budget exhausted")
        self._raise_on_process_failure(result)
        return self._last_assistant_text(result)

    def conclude(self, execution_input: ExecutionInput, *, reason: str, lease_active) -> object:
        if not self._session_id:
            raise TransientWorkerError("Explore session is unavailable for conclude")
        result = self._run(
            execution_input,
            self.prompts.render(
                "explore_conclude.md",
                {
                    "task_id": execution_input.task_id,
                    "intent_id": execution_input.intent_id,
                    "intent_description": execution_input.intent_description,
                    "run_dir": execution_input.run_dir,
                    "skill_directories": _format_skill_directories(execution_input.skill_directories),
                },
            )
            + f"\n收口原因：{reason}\n",
            lease_active=lease_active,
            timeout_seconds=execution_input.conclude_timeout_seconds
            or self.default_conclude_seconds,
            tool_names=native_tool_names("explore", "conclude"),
            rpc=True,
            enable_axiom_tools=False,
        )
        if result.cancelled:
            raise LeaseLostError(result.cancel_reason or "worker cancelled")
        if result.timed_out:
            raise ExploreExecutionTimeout("Explore conclude budget exhausted")
        self._raise_on_process_failure(result)
        return self._last_assistant_text(result)


def _message_text(message: dict[str, Any]) -> list[str]:
    content = message.get("content")
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    return [
        str(item.get("text"))
        for item in content
        if isinstance(item, dict) and item.get("type") == "text" and item.get("text")
    ]


def _format_skill_directories(paths: list[str]) -> str:
    return "\n".join(f"- `{path}`" for path in paths) or "- （当前没有可用 Skill 目录）"


def _explore_soft_reminder(remaining_seconds: int) -> str:
    return (
        "本次 Explore 执行预算已使用约 70%，"
        f"剩余约 {remaining_seconds} 秒。\n"
        "不要再扩展新的独立测试方向。请立即整理 findings.md 和 leads.md；"
        "未完成分支如实写入 leads.md。Evidence 有则保留，没有也不要补跑。"
        "宁可承认本轮未完成，也不要耗尽预算后从头重试。完成后仅返回 FINISH。"
    )


def _explore_soft_reminder_timing(execute_seconds: int) -> tuple[float, int]:
    if execute_seconds <= 0:
        raise ValueError("Explore execute budget must be positive")
    remind_after = execute_seconds * EXPLORE_SOFT_REMINDER_RATIO
    remaining = (execute_seconds * 30 + 99) // 100
    return remind_after, remaining
