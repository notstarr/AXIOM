from __future__ import annotations

import json
import os
import sys
import time
import uuid
from collections.abc import Callable
from urllib.parse import urlparse
from pathlib import Path, PurePosixPath
from typing import Any

from axiom.dispatcher.bootstrap import (
    AxiomToolLoadError,
    BootstrapExecutionTimeout,
    TransientWorkerError,
)
from axiom.dispatcher.activity import SessionActivityTracker
from axiom.dispatcher.browser_broker import (
    BROWSER_TOOL_NAMES,
    BrowserBrokerError,
    BrowserBrokerManager,
    BrowserLeaseHandle,
    HttpBrowserRunIO,
    browser_skill_directory,
)
from axiom.dispatcher.heartbeat import LeaseLostError
from axiom.dispatcher.explore import ExploreExecutionTimeout
from axiom.dispatcher.process import (
    CubePtySupervisor,
    ProcessResult,
    ProcessSpec,
    ProcessSupervisor,
    RpcProcessSpec,
    RpcProcessSupervisor,
)
from axiom.dispatcher.reason import ReasonExecutionTimeout
from axiom.dispatcher.review import ReviewExecutionTimeout
from axiom.dispatcher.tool_path import (
    build_tool_path,
    discover_tool_environment,
    prepare_runtime_bin,
)
from axiom.modules.execution.schemas import ActivityCommand, ExecutionInput, PlanExecutionInput
from axiom.modules.phase_capabilities.builtins import native_tool_names
from axiom.modules.plan.schemas import PlanWorkspace
from axiom.modules.reason.schemas import ReasonWorkspace
from axiom.modules.token_usage.schemas import TokenUsageBatchCommand
from axiom.modules.workers.pi_config import PiWorkerConfig, canonical_provider_api, sdk_base_url
from axiom.modules.workers.schemas import Worker
from axiom.modules.sandboxes.backend import CubeSandboxExecutionBackend
from axiom.modules.sandboxes.schemas import SandboxHandle


PiSettings = PiWorkerConfig
EXPLORE_SOFT_REMINDER_RATIO = 0.70
EXPLORE_RPC_STREAM_IDLE_TIMEOUT_SECONDS = 120.0
EXPLORE_RPC_STREAM_RECOVERY_PROMPT = (
    "上一轮模型响应流长时间没有完成，该轮请求已被中止。请基于当前会话、已完成的工具结果和现有 "
    "Workspace 继续执行。不要重复已经完成的测试；先检查最后一个完整 Tool Result，然后从尚未完成的"
    "步骤继续。最终仍须遵守当前 Intent、产物和 FINISH 合同。"
)


class _ExploreArtifactProgressProbe:
    """Detect durable Explore output progress without another model call."""

    def __init__(
        self,
        run_dir: str,
        timeout_seconds: int,
        *,
        scan_interval: float = 2.0,
        start_armed: bool = True,
    ):
        self.root = Path(run_dir).resolve() / "artifacts"
        self.timeout_seconds = max(30, int(timeout_seconds))
        self.scan_interval = max(0.2, float(scan_interval))
        self._last_scan = 0.0
        self._last_signature: tuple[tuple[str, int, int], ...] | None = None
        self._last_change = time.monotonic()
        self._armed = start_armed

    def mark_external_action(self) -> None:
        """Arm the no-progress timer after the first real probe/tool action."""

        if not self._armed:
            self._armed = True
            self._last_change = time.monotonic()

    def __call__(self) -> str | None:
        now = time.monotonic()
        if now - self._last_scan < self.scan_interval:
            return None
        self._last_scan = now
        if not self._armed:
            return None
        signature: list[tuple[str, int, int]] = []
        try:
            if self.root.is_dir() and not self.root.is_symlink():
                for path in self.root.rglob("*"):
                    if path.is_symlink() or not path.is_file():
                        continue
                    try:
                        stat = path.stat()
                    except OSError:
                        continue
                    signature.append((str(path.relative_to(self.root)), stat.st_size, stat.st_mtime_ns))
        except OSError:
            signature = []
        current = tuple(sorted(signature))
        if self._last_signature is None or current != self._last_signature:
            self._last_signature = current
            self._last_change = now
            return None
        if now - self._last_change >= self.timeout_seconds:
            return "explore_no_progress"
        return None
_REASON_PROMPT_PROFILE_FILES = {
    "default": "reason_default.md",
    "bootstrap": "reason_bootstrap.md",
    "progress": "reason_progress.md",
    "human": "reason_human.md",
    "continuation": "reason_continuation.md",
}
CONTEXT_ATTENTION_SYSTEM_PROMPT = (
    "在开始规划或执行任何操作前，先确认本系统提示末尾的 AXIOM INJECTED CONTEXT 注入块："
    "hints.json、knowledge.json、vulnerabilities.json 等任务上下文与 whatisvul 判定标准"
    "通常已直接注入其中，直接使用，严禁重复读取已注入的同一内容；仅当注入块明显缺失时，"
    "才按 Context Manifest 路径补读，并按需展开 Fact、Intent、Todo、Artifact、Evidence "
    "和 Goal Evaluation。Task Hint 是任务级约束，"
    "Project Knowledge 是业务背景，Skill 是执行方法，已有漏洞及 status_reason 必须用于避免"
    "重复规划、测试和上报。冲突时遵循：系统规则/Scope/阶段合同 > Task Hint > Project "
    "Knowledge > Skill 方法论 > 已有漏洞及状态原因 > 普通 Context 与工具输出。"
    "whatisvul 判定标准正文已随注入块提供，无需重复读取；仅当注入块缺失时，"
    "才运行 axiom skill path whatisvul 并完整读取返回目录中的 SKILL.md。读取失败时先运行 "
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
        browser_broker: BrowserBrokerManager | None = None,
        llm_gateway_enabled: bool = False,
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
        self.browser_broker = browser_broker
        self.llm_gateway_enabled = llm_gateway_enabled
        self._session_id: str | None = None
        self._browser_lease: BrowserLeaseHandle | None = None

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
                    "scope_guidance": _scope_guidance(execution_input.scope),
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
            enable_browser=False,
            append_injected_context=False,
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
        stream_idle_timeout_seconds: float | None = None,
        stream_recovery_prompt: str | None = None,
        semantic_progress_probe: Callable[[], str | None] | None = None,
        external_action_reporter: Callable[[], None] | None = None,
        enable_axiom_tools: bool = True,
        enable_browser: bool = True,
        append_injected_context: bool = True,
    ) -> ProcessResult:
        remote_cube = getattr(execution_input, "sandbox_backend", "local") == "cube"
        cube_backend: CubeSandboxExecutionBackend | None = None
        cube_handle: SandboxHandle | None = None
        runtime_extension_paths: dict[str, str] = {}
        if remote_cube:
            sandbox_id = getattr(execution_input, "sandbox_id", None) or ""
            if not sandbox_id:
                raise TransientWorkerError("Cube Sandbox identity is missing")
            run_root = PurePosixPath(execution_input.run_dir)
            worker_root = PurePosixPath("/workspace/axiom")
            if run_root != worker_root and worker_root not in run_root.parents:
                raise TransientWorkerError("Cube Run directory is outside /workspace/axiom")
            configured_session = (self.settings.session_dir or "").strip()
            if configured_session:
                candidate = PurePosixPath(configured_session)
                if not candidate.is_absolute() or (
                    candidate != worker_root and worker_root not in candidate.parents
                ):
                    configured_session = ""
            session_dir = Path(configured_session or str(run_root / "sessions"))
            agent_dir = session_dir.parent
            cube_backend = CubeSandboxExecutionBackend(
                api_key=os.environ.get("CUBESANDBOX_API_KEY", ""),
                base_url=os.environ.get("CUBESANDBOX_BASE_URL", ""),
            )
            cube_handle = SandboxHandle(
                sandbox_id=sandbox_id,
                root_path="/workspace/axiom",
                backend="cube",
            )
            self._write_models_file_remote(cube_backend, cube_handle, agent_dir)
            runtime_extension_paths = self._sync_cube_runtime_extensions(
                cube_backend,
                cube_handle,
            )
        else:
            session_dir = Path(
                self.settings.session_dir or Path(execution_input.run_dir) / "sessions"
            )
            session_dir.mkdir(parents=True, exist_ok=True)
            agent_dir = session_dir.parent
            self._write_models_file(agent_dir)
        environment = dict(self.settings.environment)
        phase = getattr(execution_input, "intent_kind", None) or getattr(
            execution_input, "phase", ""
        )
        environment.update(
            {
                "AXIOM_RUN_DIR": execution_input.run_dir,
                "AXIOM_BOOTSTRAP_TOOLS": str(Path(__file__).resolve().parent / "tools" / "bootstrap"),
                "AXIOM_TASK_ID": execution_input.task_id,
                "AXIOM_PROJECT_ID": execution_input.project_id,
                "AXIOM_LEASE_ID": execution_input.lease_id,
                "AXIOM_PHASE": phase,
                "AXIOM_WORKER": execution_input.worker_name,
                "AXIOM_SANDBOX_ID": getattr(execution_input, "sandbox_id", None) or "",
                "AXIOM_CONTEXT_REVISION": str(getattr(execution_input, "injected_context_revision", None) or getattr(execution_input, "context_revision", None) or ""),
                "AXIOM_CONTEXT_HASH": getattr(execution_input, "injected_context_hash", "") or "",
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
                "AXIOM_PYTHON": "python3" if remote_cube else sys.executable,
            }
        )
        # Stage capability bindings are the runtime authority.  Apply their
        # environment after the persisted model-backend config so stale legacy
        # Worker MCP settings cannot leak tools into a new Lease.
        environment.update(execution_input.capability_environment)
        intent_id = getattr(execution_input, "intent_id", None)
        reason_run_id = getattr(execution_input, "reason_run_id", None)
        review_run_id = getattr(execution_input, "review_run_id", None)
        vulnerability_id = getattr(execution_input, "vulnerability_id", None)
        if intent_id:
            environment["AXIOM_INTENT_ID"] = intent_id
        if reason_run_id:
            environment["AXIOM_REASON_RUN_ID"] = reason_run_id
        if review_run_id:
            environment["AXIOM_REVIEW_RUN_ID"] = review_run_id
        plan_run_id = getattr(execution_input, "plan_run_id", None)
        if plan_run_id:
            environment["AXIOM_PLAN_RUN_ID"] = plan_run_id
        if vulnerability_id:
            environment["AXIOM_VULNERABILITY_ID"] = vulnerability_id
        if self.service_token:
            environment["AXIOM_SERVICE_TOKEN"] = self.service_token
        if extra_environment:
            environment.update(extra_environment)
        if self.settings.base_url:
            environment["PI_BASE_URL"] = sdk_base_url(
                self.settings.provider_api, self.settings.base_url
            )
        if self.settings.provider_api:
            environment["PI_PROVIDER_API"] = canonical_provider_api(self.settings.provider_api)
        if self.settings.api_key:
            environment["PI_API_KEY"] = self.settings.api_key
        if self.llm_gateway_enabled and self.service_token:
            # 网关模式下同步改写 env，任何读取 PI_BASE_URL 的路径都走网关。
            from urllib.parse import quote

            environment["PI_BASE_URL"] = (
                f"{self.api_url}/api/v1/llm-gateway/{quote(str(self.worker.name), safe='')}"
            )
            environment["PI_API_KEY"] = self.service_token
        environment["PI_MODEL_CONTEXT_WINDOW"] = str(self.settings.context_window)
        if remote_cube:
            # Cube templates own their toolchain.  Never materialize or expose
            # the control-plane virtualenv/bin directory to a remote worker.
            environment["PATH"] = environment.get(
                "PATH", "/usr/local/bin:/usr/bin:/bin"
            )
        else:
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
        browser_enabled = False
        browser_stack = os.environ.get("AXIOM_BROWSER_STACK", "skill").strip().lower()
        if enable_browser and phase in {"bootstrap", "explore"}:
            if browser_stack != "legacy":
                # BrowserSkill 栈：注入 skill 目录与 wrapper 路径，不创建旧 broker lease。
                browser_enabled = True
                skill_dir = str(Path(__file__).resolve().parent / "skills" / "browser-skill")
                if skill_dir not in execution_input.skill_directories:
                    execution_input.skill_directories.append(skill_dir)
                environment.setdefault(
                    "AXIOM_BSK",
                    str(Path(__file__).resolve().parent / "tools" / "axiom-bsk"),
                )
            else:
                browser_skill = browser_skill_directory(
                    execution_input.skill_directories,
                    allow_remote=remote_cube,
                )
                if browser_skill is not None and self.browser_broker is not None:
                    browser_enabled = True
                    try:
                        browser_lease = self._ensure_browser_lease(
                            execution_input,
                            environment,
                        )
                    except BrowserBrokerError as exc:
                        raise TransientWorkerError(
                            f"Browser Broker初始化失败（{exc.code}）：{exc}"
                        ) from exc
                    environment.update(
                        {
                            "AXIOM_BROWSER_SOCKET": browser_lease.socket_path,
                            "AXIOM_BROWSER_TOKEN": browser_lease.token,
                            "AXIOM_BROWSER_TRANSPORT": browser_lease.transport,
                            "AXIOM_BROWSER_ENDPOINT": browser_lease.endpoint,
                            "AXIOM_BROWSER_LEASE_ID": browser_lease.lease_id,
                            "AXIOM_BROWSER_SESSION_REF": browser_lease.session_ref,
                            "AXIOM_BROWSER_GATEWAY_TOKEN": (
                                getattr(self.browser_broker, "gateway_service_token", "")
                                or self.service_token
                            ),
                        }
                    )
                    tool_names = list(dict.fromkeys([*tool_names, *BROWSER_TOOL_NAMES]))
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
            browser_enabled=browser_enabled,
            binary="pi" if remote_cube else None,
            runtime_extension_paths=runtime_extension_paths,
            injected_system_context=(
                execution_input.injected_system_context if append_injected_context else ""
            ),
        )
        tracker = None
        if (
            not remote_cube
            and (self.activity_reporter is not None or self.token_usage_reporter is not None)
        ):
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
                if semantic_progress_probe is not None:
                    reason = semantic_progress_probe()
                    if reason:
                        return reason
                return None

            cube_supervisor = None
            if remote_cube:
                assert cube_backend is not None and cube_handle is not None
                cube_supervisor = CubePtySupervisor(
                    backend=cube_backend,
                    handle=cube_handle,
                )
            if rpc:
                rpc_spec = RpcProcessSpec(
                        command=argv,
                        log_dir=Path(execution_input.run_dir) / "process",
                        timeout_seconds=timeout_seconds,
                        environment=environment,
                        prompt=prompt,
                        steer_after_seconds=steer_after_seconds,
                        steer_message=steer_message,
                        stream_idle_timeout_seconds=stream_idle_timeout_seconds,
                        stream_recovery_prompt=stream_recovery_prompt,
                        intervention_poller=self.intervention_poller if phase == "explore" else None,
                        intervention_result_reporter=self.intervention_result_reporter if phase == "explore" else None,
                        external_action_reporter=external_action_reporter,
                    )
                if cube_supervisor is not None:
                    result = cube_supervisor.run_rpc(
                        rpc_spec,
                        should_cancel=should_cancel,
                        on_steer=lambda: self._report_soft_reminder(execution_input),
                    )
                else:
                    result = self.rpc_supervisor.run(
                        rpc_spec,
                        should_cancel=should_cancel,
                        on_steer=lambda: self._report_soft_reminder(execution_input),
                    )
            else:
                process_spec = ProcessSpec(
                        command=argv,
                        log_dir=Path(execution_input.run_dir) / "process",
                        timeout_seconds=timeout_seconds,
                        environment=environment,
                    )
                result = (
                    cube_supervisor.run(process_spec, should_cancel=should_cancel)
                    if cube_supervisor is not None
                    else self.supervisor.run(process_spec, should_cancel=should_cancel)
                )
        finally:
            if tracker is not None:
                tracker.stop()
        if tracker is not None and tracker.lease_lost:
            raise LeaseLostError("Intent activity lease is no longer active")
        return result
    def close(self) -> None:
        """Release browser state after the complete claimed work lifecycle."""

        if self._browser_lease is not None and self.browser_broker is not None:
            self.browser_broker.close_lease(self._browser_lease)
        self._browser_lease = None

    def _browser_session_save_path(self) -> str:
        """登录态落盘路径：<session基目录>/<origin域名>.json，供后续 lease 按 origin 注入。"""
        origin = getattr(self, "_browser_origin", "")
        if not origin:
            return ""
        parsed = urlparse(origin if "://" in origin else f"//{origin}")
        host = (parsed.hostname or "").strip().lower().rstrip(".")
        if not host:
            return ""
        base = self._browser_session_base()
        path = Path(base) / f"{host}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        return str(path)

    def _browser_session_base() -> str:
        """快照基目录：与 Chrome-SecTest profile 同址，登录态随 profile 持久。"""
        base = os.environ.get("AXIOM_BROWSER_SESSION_DIR", "").strip()
        if base:
            return base
        if sys.platform == "darwin":
            return str((Path.home() / "Library/Application Support/Chrome-SecTest/browser-sessions").resolve())
        return str((Path.home() / ".config/Chrome-SecTest/browser-sessions").resolve())

    @staticmethod
    def _browser_session_options(origin: str = "") -> tuple[str, bool]:
        """Resolve the session snapshot for this lease.

        优先级：AXIOM_BROWSER_PERSISTENT > AXIOM_BROWSER_SESSION 显式名称 >
        按任务 origin 域名自动匹配（<域名>.json，支持后缀回退，如
        qq.com.json 匹配 tdream.qq.com）> 干净上下文。
        """
        persistent = os.environ.get("AXIOM_BROWSER_PERSISTENT", "").strip().lower() in {"1", "true"}
        if persistent:
            return "", True
        base = PiBootstrapExecutor._browser_session_base()
        if not base:
            return "", False
        base_path = Path(base)
        name = os.environ.get("AXIOM_BROWSER_SESSION", "").strip()
        if name:
            return str(base_path / f"{name}.json"), False
        origin_text = (origin or "").strip()
        host = ""
        if origin_text:
            parsed = urlparse(origin_text if "://" in origin_text else f"//{origin_text}")
            host = (parsed.hostname or "").strip().lower().rstrip(".")
        if not host:
            return "", False
        exact = base_path / f"{host}.json"
        if exact.is_file():
            return str(exact), False
        matches = [
            (len(item.stem), item)
            for item in base_path.glob("*.json")
            if "." in item.stem and host.endswith(f".{item.stem.lower()}")
        ]
        if matches:
            return str(max(matches)[1]), False
        return "", False

    def _ensure_browser_lease(
        self,
        execution_input: ExecutionInput,
        environment: dict[str, str],
    ) -> BrowserLeaseHandle:
        if self._browser_lease is not None:
            if self._browser_lease.lease_id != execution_input.lease_id:
                raise BrowserBrokerError(
                    "lease_mismatch",
                    "同一个Pi Executor不能复用不同的Browser Lease",
                )
            return self._browser_lease
        assert self.browser_broker is not None
        session_path, persistent = self._browser_session_options(
            execution_input.origin
        )
        self._browser_origin = execution_input.origin
        self._browser_run_dir = execution_input.run_dir
        handle = self.browser_broker.register_lease(
            lease_id=execution_input.lease_id,
            task_id=execution_input.task_id,
            intent_id=execution_input.intent_id or "",
            run_dir=execution_input.run_dir,
            scope=execution_input.scope,
            cdp_url=environment.get("CDP_URL", "http://127.0.0.1:9223"),
            session_path=session_path,
            persistent=persistent,
            transport="http" if getattr(execution_input, "sandbox_backend", "local") == "cube" else "unix",
            session_ref=Path(session_path).stem if session_path else "",
            run_io=(
                HttpBrowserRunIO(
                    api_url=self.api_url,
                    service_token=self.service_token,
                    task_id=execution_input.task_id,
                    owner_id=(
                        getattr(execution_input, "intent_id", None)
                        or getattr(execution_input, "reason_run_id", None)
                        or getattr(execution_input, "plan_run_id", None)
                        or getattr(execution_input, "review_run_id", None)
                        or ""
                    ),
                    lease_id=execution_input.lease_id,
                    root=execution_input.run_dir,
                    max_file_bytes=getattr(
                        self.browser_broker,
                        "file_bridge_limit",
                        2 * 1024 * 1024,
                    ),
                )
                if getattr(execution_input, "sandbox_backend", "local") == "cube"
                else None
            ),
        )
        self._browser_lease = handle
        return handle

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
        browser_enabled: bool = False,
        injected_system_context: str = "",
        binary: str | None = None,
        runtime_extension_paths: dict[str, str] | None = None,
    ) -> list[str]:
        attention_prompt = (
            CONTEXT_ATTENTION_SYSTEM_PROMPT
            if "bash" in tool_names
            else NO_BASH_CONTEXT_ATTENTION_SYSTEM_PROMPT
        )
        if injected_system_context.strip():
            attention_prompt = attention_prompt.rstrip() + "\n\n" + injected_system_context.strip()
        argv = [
            binary or self.settings.binary,
            "--provider",
            self.settings.provider,
            "--model",
            self.settings.model,
            "--session-dir",
            str(session_dir),
            "--mode",
            mode,
            "--append-system-prompt",
            attention_prompt,
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
        runtime_paths = runtime_extension_paths or {}
        if guard_enabled:
            extension = runtime_paths.get("axiom-guard") or str(
                Path(__file__).with_name("extensions") / "axiom-guard" / "index.ts"
            )
            argv.extend(["--extension", str(extension)])
        if selected_mcp_tools:
            extension = runtime_paths.get("axiom-mcp-tools") or str(
                Path(__file__).with_name("extensions")
                / "axiom-mcp-tools"
                / "index.ts"
            )
            argv.extend(["--extension", str(extension)])
        if browser_enabled:
            extension = runtime_paths.get("axiom-browser") or str(
                Path(__file__).with_name("extensions")
                / "axiom-browser"
                / "index.ts"
            )
            argv.extend(["--extension", str(extension)])
        selected_extensions = list(dict.fromkeys(axiom_tool_extensions or []))
        for extension in selected_extensions:
            argv.extend(["--extension", extension])
        if selected_keys:
            activation = runtime_paths.get("axiom-tool-activation") or str(
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
        payload = self._models_payload()
        if payload is None:
            return
        agent_dir.mkdir(parents=True, exist_ok=True)
        path = agent_dir / "models.json"
        temporary = agent_dir / ".models.json.tmp"
        temporary.write_text(
            json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        temporary.replace(path)

    def _models_payload(self) -> dict[str, Any] | None:
        if not self.settings.base_url or not self.settings.model:
            return None
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
            # Pi intentionally requires explicit model opt-in for the two
            # levels above high.  Selecting one in AXIOM is that opt-in;
            # an upstream provider may still reject a level it does not expose.
            model["thinkingLevelMap"] = {
                self.settings.thinking_level: self.settings.thinking_level
            }
        provider: dict[str, Any] = {
            "baseUrl": sdk_base_url(self.settings.provider_api, self.settings.base_url),
            "api": canonical_provider_api(self.settings.provider_api),
            "apiKey": self.settings.api_key or "none",
            "models": [model],
        }
        if self.llm_gateway_enabled and self.service_token:
            # LLM 代理模式：LLM 请求改走 serve 网关（代理只作用于 serve 这一跳），
            # 真实 base_url / api_key 不再下发到 Worker 进程。
            from urllib.parse import quote

            provider["baseUrl"] = (
                f"{self.api_url}/api/v1/llm-gateway/{quote(str(self.worker.name), safe='')}"
            )
            provider["apiKey"] = self.service_token
        return {"providers": {self.settings.provider: provider}}

    def _write_models_file_remote(
        self,
        backend: CubeSandboxExecutionBackend,
        handle: SandboxHandle,
        agent_dir: Path,
    ) -> None:
        payload = self._models_payload()
        if payload is None:
            return
        backend.write_file(
            handle,
            destination=str(agent_dir / "models.json"),
            content=json.dumps(
                payload,
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("utf-8"),
        )

    @staticmethod
    def _sync_cube_runtime_extensions(
        backend: CubeSandboxExecutionBackend,
        handle: SandboxHandle,
    ) -> dict[str, str]:
        """Copy runner-owned TypeScript extensions into the Task Cube.

        The browser process stays on the runner, but the extension code is
        loaded by Pi inside Cube.  Using the source-tree path in ``--extension``
        would leak a host path and fail at runtime, so copy the small runtime
        package under the fixed worker root and return Cube-visible paths.
        """

        dispatcher_root = Path(__file__).resolve().parent
        remote_root = PurePosixPath("/workspace/axiom/shared/runtime")
        source_roots = [
            dispatcher_root / "extensions" / name
            for name in (
                "axiom-guard",
                "axiom-mcp-tools",
                "axiom-browser",
                "axiom-tool-activation",
            )
        ]
        source_roots.append(dispatcher_root / "axiom_tools")
        for source_root in source_roots:
            if not source_root.is_dir():
                raise TransientWorkerError(
                    f"Cube runtime extension source is missing: {source_root.name}"
                )
            relative_root = source_root.relative_to(dispatcher_root)
            for source in source_root.rglob("*"):
                if not source.is_file() or source.is_symlink():
                    continue
                destination = remote_root / relative_root / source.relative_to(source_root)
                if backend.exists(handle, path=str(destination)):
                    continue
                backend.upload_file(
                    handle,
                    source=source,
                    destination=str(destination),
                )
        return {
            name: str(remote_root / "extensions" / name / "index.ts")
            for name in (
                "axiom-guard",
                "axiom-mcp-tools",
                "axiom-browser",
                "axiom-tool-activation",
            )
        }

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
        provider_error = _terminal_provider_error(result.stdout)
        if provider_error:
            raise TransientWorkerError(provider_error[-2_000:])
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
            self._build_reason_prompt(execution_input),
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

    def _build_reason_prompt(self, execution_input: ExecutionInput) -> str:
        values = {
            "task_id": execution_input.task_id,
            "reason_run_id": execution_input.reason_run_id or "",
            "intent_description": execution_input.intent_description,
            "source_fact_ids": json.dumps(
                execution_input.intent_source_facts,
                ensure_ascii=False,
            ),
            "origin": execution_input.origin,
            "goal": execution_input.goal,
            "target_guidance": _reason_target_guidance(execution_input.scope),
            "context_manifest_path": execution_input.context_manifest_path,
            "run_dir": execution_input.run_dir,
            "skill_directories": _format_skill_directories(execution_input.skill_directories),
        }
        profile = getattr(execution_input, "reason_prompt_profile", "default") or "default"
        profile_file = _REASON_PROMPT_PROFILE_FILES.get(profile, "reason_default.md")
        return (
            self.prompts.render("reason.md", values)
            + "\n\n---\n\n"
            + f"# 本轮 Reason 场景策略（由 AXIOM 代码固定注入：{profile}）\n\n"
            + self.prompts.render(profile_file, {})
        )

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
                    "target_guidance": _reason_target_guidance(execution_input.scope),
                },
            )
            + f"\n收口原因：{reason}\n",
            lease_active=lease_active,
            timeout_seconds=execution_input.conclude_timeout_seconds
            or self.default_conclude_seconds,
            tool_names=native_tool_names("reason", "conclude"),
            extra_environment={"AXIOM_REASON_WORKSPACE_ID": workspace.id},
            enable_axiom_tools=False,
            append_injected_context=False,
        )
        if result.cancelled:
            raise LeaseLostError(result.cancel_reason or "worker cancelled")
        if result.timed_out:
            raise ReasonExecutionTimeout("Reason conclude budget exhausted")
        self._raise_on_process_failure(result)
        return self._last_assistant_text(result)


class PiPlanExecutor(PiBootstrapExecutor):
    """Independent Pi adapter for the Plan phase.

    The process/lease transport is shared with the low-level Pi adapter, but
    Plan has its own input type, workspace identity, prompt files, tool scope,
    and environment contract.  It never renders or dispatches a Reason
    prompt and never exposes a Reason workspace to the worker.
    """

    def execute(
        self,
        execution_input: PlanExecutionInput,
        *,
        workspace: PlanWorkspace,
        lease_active,
    ) -> str:
        if not execution_input.context_manifest_path:
            raise TransientWorkerError("Plan Context manifest is unavailable")
        if workspace.plan_run_id != execution_input.plan_run_id:
            raise TransientWorkerError("Plan Workspace identity does not match PlanRun")
        self._session_id = self._session_id or uuid.uuid4().hex
        result = self._run(
            execution_input,
            self._build_plan_prompt(execution_input),
            lease_active=lease_active,
            timeout_seconds=execution_input.execute_timeout_seconds or 900,
            tool_names=native_tool_names("plan", "execute"),
            extra_environment={"AXIOM_PLAN_WORKSPACE_ID": workspace.id},
            enable_browser=False,
        )
        if result.cancelled:
            raise LeaseLostError(result.cancel_reason or "worker cancelled")
        if result.timed_out:
            from axiom.dispatcher.plan import PlanExecutionTimeout

            raise PlanExecutionTimeout("Plan execute budget exhausted")
        self._raise_on_process_failure(result)
        return self._last_assistant_text(result)

    def _build_plan_prompt(self, execution_input: PlanExecutionInput) -> str:
        return self.prompts.render(
            "plan.md",
            {
                "task_id": execution_input.task_id,
                "plan_run_id": execution_input.plan_run_id,
                "origin": execution_input.origin,
                "goal": execution_input.goal,
                "context_manifest_path": execution_input.context_manifest_path or "",
                "run_dir": execution_input.run_dir,
                "skill_directories": _format_skill_directories(
                    execution_input.skill_directories
                ),
            },
        )

    def conclude(
        self,
        execution_input: PlanExecutionInput,
        *,
        workspace: PlanWorkspace,
        reason: str,
        lease_active,
    ) -> str:
        if not self._session_id:
            raise TransientWorkerError("Plan session is unavailable for conclude")
        if workspace.plan_run_id != execution_input.plan_run_id:
            raise TransientWorkerError("Plan Workspace identity does not match PlanRun")
        result = self._run(
            execution_input,
            self.prompts.render(
                "plan_conclude.md",
                {
                    "task_id": execution_input.task_id,
                    "plan_run_id": execution_input.plan_run_id,
                    "workspace_id": workspace.id,
                    "run_dir": execution_input.run_dir,
                },
            )
            + f"\n收口原因：{reason}\n",
            lease_active=lease_active,
            timeout_seconds=execution_input.conclude_timeout_seconds or 180,
            tool_names=native_tool_names("plan", "conclude"),
            extra_environment={"AXIOM_PLAN_WORKSPACE_ID": workspace.id},
            enable_browser=False,
            append_injected_context=False,
        )
        if result.cancelled:
            raise LeaseLostError(result.cancel_reason or "worker cancelled")
        if result.timed_out:
            from axiom.dispatcher.plan import PlanExecutionTimeout

            raise PlanExecutionTimeout("Plan conclude budget exhausted")
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
        progress_probe = _ExploreArtifactProgressProbe(
            execution_input.run_dir,
            execution_input.explore_no_progress_seconds,
            start_armed=False,
        )
        result = self._run(
            execution_input,
            self.prompts.render(
                "explore.md",
                {
                    "task_id": execution_input.task_id,
                    "intent_id": execution_input.intent_id,
                    "todo_id": execution_input.todo_id or "无",
                    "intent_description": execution_input.intent_description,
                    "intent_targets": _intent_targets_text(execution_input.intent_targets),
                    "origin": execution_input.origin,
                    "goal": execution_input.goal,
                    "scope_guidance": _scope_guidance(execution_input.scope),
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
            stream_idle_timeout_seconds=EXPLORE_RPC_STREAM_IDLE_TIMEOUT_SECONDS,
            stream_recovery_prompt=EXPLORE_RPC_STREAM_RECOVERY_PROMPT,
            semantic_progress_probe=progress_probe,
            external_action_reporter=progress_probe.mark_external_action,
        )
        if result.cancelled:
            if result.cancel_reason == "explore_no_progress":
                raise ExploreExecutionTimeout(
                    "Explore 持续无新产物，已由代码触发 bounded conclude"
                )
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
                    "skill_directories": _format_skill_directories(
                        execution_input.skill_directories
                    ),
                },
            )
            + f"\n收口原因：{reason}\n",
            lease_active=lease_active,
            timeout_seconds=execution_input.conclude_timeout_seconds
            or self.default_conclude_seconds,
            tool_names=native_tool_names("explore", "conclude"),
            rpc=True,
            enable_axiom_tools=False,
            append_injected_context=False,
        )
        if result.cancelled:
            raise LeaseLostError(result.cancel_reason or "worker cancelled")
        if result.timed_out:
            raise ExploreExecutionTimeout("Explore conclude budget exhausted")
        self._raise_on_process_failure(result)
        return self._last_assistant_text(result)


class PiReviewExecutor(PiBootstrapExecutor):
    """Pi Reviewer adapter with one JSON session across execute and conclude."""

    def execute(self, execution_input: ExecutionInput, *, lease_active) -> object:
        if not execution_input.context_manifest_path:
            raise TransientWorkerError("Reviewer Context manifest is unavailable")
        if not execution_input.review_run_id or not execution_input.vulnerability_id:
            raise TransientWorkerError("Reviewer execution identity is incomplete")
        self._session_id = self._session_id or uuid.uuid4().hex
        result = self._run(
            execution_input,
            self.prompts.render(
                "review.md",
                {
                    "task_id": execution_input.task_id,
                    "review_run_id": execution_input.review_run_id,
                    "vulnerability_id": execution_input.vulnerability_id,
                    "review_reason": execution_input.intent_description,
                    "origin": execution_input.origin,
                    "goal": execution_input.goal,
                    "scope_guidance": _scope_guidance(execution_input.scope),
                    "context_manifest_path": execution_input.context_manifest_path,
                    "run_dir": execution_input.run_dir,
                    "skill_directories": _format_skill_directories(
                        execution_input.skill_directories
                    ),
                },
            ),
            lease_active=lease_active,
            timeout_seconds=execution_input.execute_timeout_seconds or 300,
            tool_names=native_tool_names("review", "execute"),
        )
        if result.cancelled:
            raise LeaseLostError(result.cancel_reason or "worker cancelled")
        if result.timed_out:
            raise ReviewExecutionTimeout("Reviewer execution budget exhausted")
        self._raise_on_process_failure(result)
        return self._last_assistant_text(result)

    def conclude(
        self,
        execution_input: ExecutionInput,
        *,
        reason: str,
        lease_active,
    ) -> object:
        if not self._session_id:
            raise TransientWorkerError("Reviewer session is unavailable for conclude")
        result = self._run(
            execution_input,
            self.prompts.render(
                "review_conclude.md",
                {
                    "task_id": execution_input.task_id,
                    "review_run_id": execution_input.review_run_id or "",
                    "vulnerability_id": execution_input.vulnerability_id or "",
                    "run_dir": execution_input.run_dir,
                },
            )
            + f"\n收口原因：{reason}\n",
            lease_active=lease_active,
            timeout_seconds=execution_input.conclude_timeout_seconds
            or self.default_conclude_seconds,
            tool_names=native_tool_names("review", "conclude"),
            enable_axiom_tools=False,
            append_injected_context=False,
        )
        if result.cancelled:
            raise LeaseLostError(result.cancel_reason or "worker cancelled")
        if result.timed_out:
            raise ReviewExecutionTimeout("Reviewer conclude budget exhausted")
        self._raise_on_process_failure(result)
        return self._last_assistant_text(result)


def _scope_guidance(scope: dict[str, object]) -> str:
    if not scope or scope.get("enabled") is False:
        return ""
    allow = [str(item) for item in scope.get("targets", []) if str(item).strip()]
    deny = [str(item) for item in scope.get("exclude", []) if str(item).strip()]
    if allow and deny:
        detail = f"白名单：{', '.join(allow)}；黑名单：{', '.join(deny)}（黑名单优先）"
    elif allow:
        detail = f"白名单：{', '.join(allow)}"
    else:
        detail = f"黑名单：{', '.join(deny)}；其余网络目标允许"
    return f"- 网络范围（Scope）：{detail}"


def _reason_target_guidance(scope: dict[str, object]) -> str:
    if not scope or scope.get("enabled") is False:
        return (
            "本任务未配置网络黑白名单；创建 Explore Intent 时可以省略 --target，"
            "不得从 Origin 推导或补造 Target。"
        )
    return (
        "本任务配置了网络黑白名单；每个新 Explore Intent 必须至少提供一个 --target。"
        "URL、域名和 IP 必须位于 Scope 内；AppID、页面路由、Artifact、安装包等逻辑 Target "
        "可以使用 miniapp:/page:/artifact:/package: 等明确前缀，但它们不会扩大网络权限。"
    )


def _intent_targets_text(targets: list[str]) -> str:
    if not targets:
        return "未指定独立 Target；按 Intent 描述和 Origin 初始信息执行"
    return json.dumps(targets, ensure_ascii=False)


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


def _terminal_provider_error(stdout: str) -> str:
    """Return Pi JSON mode's final provider error, ignoring recovered retries."""

    terminal_error = ""
    for line in stdout.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue

        messages: list[dict[str, Any]] = []
        message = payload.get("message")
        if isinstance(message, dict):
            messages.append(message)
        if payload.get("type") == "agent_end" and isinstance(payload.get("messages"), list):
            messages.extend(
                item for item in payload["messages"] if isinstance(item, dict)
            )
        for assistant in messages:
            if assistant.get("role") != "assistant":
                continue
            if assistant.get("stopReason") == "error":
                error = assistant.get("errorMessage")
                if isinstance(error, str) and error.strip():
                    terminal_error = error.strip()
                else:
                    text = "\n".join(_message_text(assistant)).strip()
                    terminal_error = text or "Pi provider returned an error response"
            else:
                terminal_error = ""

        if payload.get("type") == "auto_retry_end":
            if payload.get("success") is True:
                terminal_error = ""
            else:
                error = payload.get("finalError")
                terminal_error = (
                    error.strip()
                    if isinstance(error, str) and error.strip()
                    else terminal_error or "Pi provider retries were exhausted"
                )
    return terminal_error


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
