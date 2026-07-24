from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from axiom.dispatcher.bootstrap import (
    BootstrapExecutionTimeout,
    TransientWorkerError,
)
from axiom.dispatcher.activity import SessionActivityTracker
from axiom.dispatcher.heartbeat import LeaseLostError
from axiom.dispatcher.explore import ExploreExecutionTimeout
from axiom.dispatcher.process import ProcessResult, ProcessSpec, ProcessSupervisor
from axiom.dispatcher.reason import ReasonExecutionTimeout, ReasonPlanError
from axiom.dispatcher.tool_path import build_tool_path
from axiom.modules.artifacts.schemas import BootstrapBundle
from axiom.modules.execution.schemas import ActivityCommand, ExecutionInput
from axiom.modules.reason.schemas import WorkspaceDraft
from axiom.modules.workers.pi_config import PiWorkerConfig
from axiom.modules.workers.schemas import Worker


PiSettings = PiWorkerConfig


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
        prompt_renderer: PromptRenderer | None = None,
        default_conclude_seconds: int = 180,
        activity_reporter: Callable[[ActivityCommand], None] | None = None,
    ):
        self.worker = worker
        self.settings = PiSettings.model_validate(worker.config)
        self.supervisor = supervisor or ProcessSupervisor()
        self.prompts = prompt_renderer or PromptRenderer()
        self.default_conclude_seconds = default_conclude_seconds
        self.activity_reporter = activity_reporter
        self._session_id: str | None = None

    def execute(self, execution_input: ExecutionInput, *, lease_active) -> object:
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
                    "run_dir": execution_input.run_dir,
                    "skill_directories": _format_skill_directories(execution_input.skill_directories),
                },
            ),
            lease_active=lease_active,
            timeout_seconds=execution_input.execute_timeout_seconds or 900,
            tool_names=self.settings.tools,
        )
        if result.cancelled:
            raise LeaseLostError(result.cancel_reason or "worker cancelled")
        if result.timed_out:
            raise BootstrapExecutionTimeout("Bootstrap execute budget exhausted")
        self._raise_on_process_failure(result)
        return self._output(execution_input, self._response_text(result))

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
            tool_names=["read", "write", "edit"],
        )
        if result.cancelled:
            raise LeaseLostError(result.cancel_reason or "worker cancelled")
        if result.timed_out:
            raise BootstrapExecutionTimeout("Bootstrap conclude budget exhausted")
        self._raise_on_process_failure(result)
        return self._output(execution_input, self._response_text(result))

    def _run(
        self,
        execution_input: ExecutionInput,
        prompt: str,
        *,
        lease_active,
        timeout_seconds: int,
        tool_names: list[str],
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
                "AXIOM_INTENT_ID": execution_input.intent_id,
                "AXIOM_PHASE": execution_input.intent_kind,
                "AXIOM_WORKER": execution_input.worker_name,
                "AXIOM_GUARD_ENABLED": "1" if execution_input.guard_enabled else "0",
                "PI_CODING_AGENT_DIR": str(agent_dir),
                "AXIOM_SKILL_DIRECTORIES": ":".join(execution_input.skill_directories),
            }
        )
        if self.settings.base_url:
            environment["PI_BASE_URL"] = self.settings.base_url
        if self.settings.provider_api:
            environment["PI_PROVIDER_API"] = self.settings.provider_api
        if self.settings.api_key:
            environment["PI_API_KEY"] = self.settings.api_key
        environment["PI_MODEL_CONTEXT_WINDOW"] = str(self.settings.context_window)
        environment.setdefault("AXIOM_SERVER", "http://127.0.0.1:8899")
        environment["PATH"] = build_tool_path(
            inherited=environment.get("PATH", os.environ.get("PATH", "")),
            configured=environment.get("AXIOM_TOOL_PATH"),
        )
        argv = self._build_argv(
            prompt,
            session_dir,
            tool_names,
            skill_paths=execution_input.skill_directories,
            guard_enabled=execution_input.guard_enabled,
        )
        tracker = (
            SessionActivityTracker(session_dir, execution_input.lease_id, self.activity_reporter)
            if self.activity_reporter is not None
            else None
        )
        if tracker is not None:
            tracker.start()
        try:
            result = self.supervisor.run(
                ProcessSpec(
                    command=argv,
                    log_dir=Path(execution_input.run_dir) / "process",
                    timeout_seconds=timeout_seconds,
                    environment=environment,
                ),
                should_cancel=lambda: (
                    "lease_lost"
                    if (tracker is not None and tracker.lease_lost) or not lease_active()
                    else None
                ),
            )
        finally:
            if tracker is not None:
                tracker.stop()
        if tracker is not None and tracker.lease_lost:
            raise LeaseLostError("Intent activity lease is no longer active")
        return result

    def _build_argv(
        self,
        prompt: str,
        session_dir: Path,
        tool_names: list[str],
        *,
        skill_paths: list[str] | None = None,
        guard_enabled: bool = False,
    ) -> list[str]:
        argv = [
            self.settings.binary,
            "--provider",
            self.settings.provider,
            "--model",
            self.settings.model,
            "--session-dir",
            str(session_dir),
        ]
        if self._session_id:
            argv.extend([self.settings.session_flag, self._session_id])
        if self.settings.thinking and self.settings.thinking != "off":
            argv.extend(["--thinking", self.settings.thinking])
        if tool_names:
            argv.extend(["--tools", ",".join(tool_names)])
        for path in dict.fromkeys([*self.settings.skill_paths, *(skill_paths or [])]):
            argv.extend(["--skill", path])
        if guard_enabled:
            extension = Path(__file__).with_name("extensions") / "axiom-guard" / "index.ts"
            argv.extend(["--extension", str(extension)])
        argv.extend(["-p", prompt])
        return argv

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
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "Pi exited unsuccessfully"
            raise TransientWorkerError(detail[-2_000:])

    @staticmethod
    def _response_text(result: ProcessResult) -> str:
        events = []
        for line in result.stdout.splitlines():
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                events.append(payload)
        texts: list[str] = []
        for event in events:
            message = event.get("message")
            if isinstance(message, dict) and message.get("role") == "assistant":
                texts.extend(_message_text(message))
            if event.get("type") == "text_end" and isinstance(event.get("content"), str):
                texts.append(event["content"])
        return "\n".join(dict.fromkeys(text.strip() for text in texts if text.strip())) or result.stdout.strip()

    @staticmethod
    def _output(execution_input: ExecutionInput, response_text: str) -> dict[str, Any]:
        bundle_path = Path(execution_input.run_dir) / "bootstrap_artifacts.json"
        try:
            raw = json.loads(bundle_path.read_text(encoding="utf-8"))
            bundle = BootstrapBundle.model_validate(raw)
        except (OSError, json.JSONDecodeError, ValueError):
            bundle = {}
        return {"summary": response_text, "artifacts": bundle}


class PiReasonExecutor(PiBootstrapExecutor):
    """File-first Reason adapter; only a validated WorkspaceDraft can escape."""

    def execute(self, execution_input: ExecutionInput, *, lease_active) -> WorkspaceDraft:
        if not execution_input.context_manifest_path:
            raise TransientWorkerError("Reason Context manifest is unavailable")
        self._session_id = self._session_id or uuid.uuid4().hex
        plan_path = Path(execution_input.run_dir) / "reason_plan.json"
        plan_path.unlink(missing_ok=True)
        result = self._run(
            execution_input,
            self.prompts.render(
                "reason.md",
                {
                    "task_id": execution_input.task_id,
                    "intent_id": execution_input.intent_id,
                    "intent_description": execution_input.intent_description,
                    "origin": execution_input.origin,
                    "goal": execution_input.goal,
                    "context_manifest_path": execution_input.context_manifest_path,
                    "run_dir": execution_input.run_dir,
                    "skill_directories": _format_skill_directories(execution_input.skill_directories),
                },
            ),
            lease_active=lease_active,
            timeout_seconds=execution_input.execute_timeout_seconds or 600,
            tool_names=self.settings.tools,
        )
        if result.cancelled:
            raise LeaseLostError(result.cancel_reason or "worker cancelled")
        if result.timed_out:
            raise ReasonExecutionTimeout("Reason execution budget exhausted")
        self._raise_on_process_failure(result)
        try:
            return WorkspaceDraft.model_validate_json(plan_path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ReasonPlanError("Reason did not produce reason_plan.json") from exc
        except ValueError as exc:
            raise ReasonPlanError("Reason produced an invalid reason_plan.json") from exc


class PiExploreExecutor(PiBootstrapExecutor):
    """Pi Explore adapter with one execute and at most one bounded conclude."""

    def execute(self, execution_input: ExecutionInput, *, lease_active) -> object:
        if not execution_input.context_manifest_path:
            raise TransientWorkerError("Explore Context manifest is unavailable")
        self._session_id = self._session_id or uuid.uuid4().hex
        result_path = Path(execution_input.run_dir) / "explore_result.json"
        result_path.unlink(missing_ok=True)
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
            timeout_seconds=execution_input.execute_timeout_seconds or 900,
            tool_names=self.settings.tools,
        )
        if result.cancelled:
            raise LeaseLostError(result.cancel_reason or "worker cancelled")
        if result.timed_out:
            raise ExploreExecutionTimeout("Explore execute budget exhausted")
        self._raise_on_process_failure(result)
        return self._read_result(result_path)

    def conclude(self, execution_input: ExecutionInput, *, reason: str, lease_active) -> object:
        if not self._session_id:
            raise TransientWorkerError("Explore session is unavailable for conclude")
        result_path = Path(execution_input.run_dir) / "explore_result.json"
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
            tool_names=["read", "write", "edit"],
        )
        if result.cancelled:
            raise LeaseLostError(result.cancel_reason or "worker cancelled")
        if result.timed_out:
            raise ExploreExecutionTimeout("Explore conclude budget exhausted")
        self._raise_on_process_failure(result)
        return self._read_result(result_path)

    @staticmethod
    def _read_result(path: Path) -> object:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}


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
