from __future__ import annotations

import json
import logging
import os
import queue
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


_RPC_PROMPT_ID = "axiom-initial-prompt"
_RPC_STEER_ID = "axiom-soft-reminder"
_RPC_FINAL_TEXT_ID = "axiom-final-text"
_RPC_ABORT_ID = "axiom-abort"
_RPC_STREAM_RECOVERY_ABORT_ID = "axiom-stream-recovery-abort"
_RPC_STREAM_RECOVERY_PROMPT_ID = "axiom-stream-recovery-prompt"
_RPC_INTERVENTION_PREFIX = "axiom-human-intervention:"
_RPC_CONTROL_EVENT_TYPES = frozenset(
    {
        "response",
        "agent_end",
        "agent_settled",
        "auto_retry_start",
        "auto_retry_end",
    }
)
_RPC_OMITTED_LOG_EVENT_TYPES = frozenset(
    {"message_update", "tool_execution_update", "bash_execution_update"}
)
_RPC_PROGRESS_EVENT_TYPES = frozenset(
    {
        "message_start",
        "message_end",
        "message_update",
        "turn_end",
        "tool_execution_start",
        "tool_execution_end",
        "tool_execution_update",
        "bash_execution_start",
        "bash_execution_end",
        "bash_execution_update",
    }
)


class ProcessLaunchError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ProcessSpec:
    command: list[str]
    log_dir: Path
    timeout_seconds: float
    environment: dict[str, str] = field(default_factory=dict)
    cwd: Path | None = None
    terminate_grace_seconds: float = 3.0
    poll_interval_seconds: float = 0.1


@dataclass(frozen=True, slots=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str
    stdout_path: Path
    stderr_path: Path
    timed_out: bool = False
    cancelled: bool = False
    cancel_reason: str | None = None
    last_assistant_text: str | None = None
    rpc_stream_stalled: bool = False


@dataclass(frozen=True, slots=True)
class RpcProcessSpec(ProcessSpec):
    prompt: str = ""
    steer_after_seconds: float | None = None
    # Optional semantic progress watchdog.  It is deliberately disabled by
    # default so JSON/RPC callers other than Explore retain their current
    # timeout semantics.
    stream_idle_timeout_seconds: float | None = None
    # When configured, one silent Assistant provider turn may be aborted and
    # resumed in the same Pi process/session.  Callers that omit the recovery
    # prompt retain the previous fail-fast watchdog behavior.
    stream_recovery_prompt: str | None = None
    stream_recovery_abort_timeout_seconds: float = 8.0
    steer_message: str | None = None
    provider_failure_grace_seconds: float = 5.0
    intervention_poller: Callable[[], list[tuple[str, str]]] | None = None
    intervention_result_reporter: Callable[[str, bool, str], None] | None = None
    intervention_poll_interval_seconds: float = 1.0
    # Called only for real tool/binary/browser actions.  Context reads and
    # assistant text streaming deliberately do not arm semantic Explore
    # progress watchdogs.
    external_action_reporter: Callable[[], None] | None = None


class ProcessSupervisor:
    """Run one worker process with a hard deadline and process-group cleanup."""

    def __init__(self, *, output_limit_bytes: int = 4 * 1024 * 1024):
        if output_limit_bytes <= 0:
            raise ValueError("output_limit_bytes must be positive")
        self.output_limit_bytes = output_limit_bytes

    def run(
        self,
        spec: ProcessSpec,
        *,
        should_cancel: Callable[[], str | None] | None = None,
        on_timeout: Callable[[], None] | None = None,
    ) -> ProcessResult:
        self._validate(spec)
        spec.log_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = spec.log_dir / "stdout.log"
        stderr_path = spec.log_dir / "stderr.log"
        environment = os.environ.copy()
        environment.update(spec.environment)
        try:
            with (
                stdout_path.open("wb") as stdout,
                stderr_path.open("wb") as stderr,
            ):
                process = subprocess.Popen(
                    spec.command,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    cwd=spec.cwd,
                    env=environment,
                    start_new_session=True,
                )
                timed_out = False
                cancelled = False
                cancel_reason: str | None = None
                deadline = time.monotonic() + spec.timeout_seconds
                while process.poll() is None:
                    if should_cancel is not None:
                        cancel_reason = should_cancel()
                        if cancel_reason:
                            cancelled = True
                            self._terminate(process, spec.terminate_grace_seconds)
                            break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        if on_timeout is not None:
                            on_timeout()
                        self._terminate(process, spec.terminate_grace_seconds)
                        break
                    time.sleep(min(spec.poll_interval_seconds, remaining))
                if process.poll() is None:
                    self._terminate(process, spec.terminate_grace_seconds)
                returncode = process.wait(timeout=spec.terminate_grace_seconds + 1)
        except OSError as exc:
            raise ProcessLaunchError(f"failed to start worker process: {spec.command[0]}") from exc

        if timed_out and returncode == 0:
            returncode = 124
        return ProcessResult(
            returncode=returncode,
            stdout=_read_tail(stdout_path, self.output_limit_bytes),
            stderr=_read_tail(stderr_path, self.output_limit_bytes),
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            timed_out=timed_out,
            cancelled=cancelled,
            cancel_reason=cancel_reason,
        )

    @staticmethod
    def _validate(spec: ProcessSpec) -> None:
        if not spec.command or not all(str(item).strip() for item in spec.command):
            raise ValueError("process command must not be empty")
        if spec.timeout_seconds <= 0:
            raise ValueError("process timeout must be positive")
        if spec.terminate_grace_seconds <= 0 or spec.poll_interval_seconds <= 0:
            raise ValueError("process timing values must be positive")

    @staticmethod
    def _terminate(process: subprocess.Popen, grace_seconds: float) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except OSError:
            process.terminate()
        try:
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                return
            except OSError:
                process.kill()


class RpcProcessSupervisor(ProcessSupervisor):
    """Run Pi RPC mode over JSONL stdin/stdout with one optional steer."""

    def run(
        self,
        spec: RpcProcessSpec,
        *,
        should_cancel: Callable[[], str | None] | None = None,
        on_steer: Callable[[], None] | None = None,
    ) -> ProcessResult:
        self._validate_rpc(spec)
        spec.log_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = spec.log_dir / "stdout.log"
        stderr_path = spec.log_dir / "stderr.log"
        environment = os.environ.copy()
        environment.update(spec.environment)

        timed_out = False
        cancelled = False
        cancel_reason: str | None = None
        completed = False
        settled = False
        final_text_requested = False
        protocol_error = ""
        provider_error = ""
        provider_failure_deadline: float | None = None
        last_assistant_text: str | None = None
        rpc_stream_stalled = False
        last_progress_at = 0.0
        assistant_stream_active = False
        active_tool_calls: set[str] = set()
        last_event_type = ""
        last_tool_name = ""
        last_tool_started_at: float | None = None
        last_tool_finished_at: float | None = None
        soft_reminder_acknowledged = False
        agent_end_seen = False
        agent_settled_seen = False
        stream_recovery_attempted = False
        stream_recovery_state = "none"
        stream_recovery_abort_acknowledged = False
        stream_recovery_settled = False
        stream_recovery_deadline: float | None = None
        reader_done = False
        events: queue.Queue[dict[str, Any] | None] = queue.Queue()
        process: subprocess.Popen[bytes] | None = None
        reader: threading.Thread | None = None

        try:
            with (
                stdout_path.open("wb") as stdout_log,
                stderr_path.open("wb") as stderr_log,
            ):
                process = subprocess.Popen(
                    spec.command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=stderr_log,
                    cwd=spec.cwd,
                    env=environment,
                    start_new_session=True,
                    bufsize=0,
                )
                assert process.stdin is not None
                assert process.stdout is not None
                reader = threading.Thread(
                    target=_read_rpc_lines,
                    args=(process.stdout, stdout_log, events),
                    name=f"rpc-output-{process.pid}",
                    daemon=True,
                )
                reader.start()

                try:
                    _send_rpc(
                        process,
                        {
                            "id": _RPC_PROMPT_ID,
                            "type": "prompt",
                            "message": spec.prompt,
                        },
                    )
                except OSError as exc:
                    protocol_error = f"failed to send Pi RPC prompt: {exc}"

                started = time.monotonic()
                last_progress_at = started
                deadline = started + spec.timeout_seconds
                steer_deadline = (
                    started + spec.steer_after_seconds
                    if spec.steer_after_seconds is not None
                    else None
                )
                steer_attempted = False
                next_intervention_poll = started
                intervention_commands: dict[str, str] = {}

                def resume_after_stream_abort() -> str:
                    nonlocal assistant_stream_active
                    nonlocal agent_end_seen
                    nonlocal agent_settled_seen
                    nonlocal last_event_type
                    nonlocal last_progress_at
                    nonlocal provider_error
                    nonlocal provider_failure_deadline
                    nonlocal settled
                    nonlocal stream_recovery_deadline
                    nonlocal stream_recovery_settled
                    nonlocal stream_recovery_state

                    if not (
                        stream_recovery_abort_acknowledged
                        and stream_recovery_settled
                    ):
                        return ""
                    try:
                        _send_rpc(
                            process,
                            {
                                "id": _RPC_STREAM_RECOVERY_PROMPT_ID,
                                "type": "prompt",
                                "message": spec.stream_recovery_prompt,
                            },
                        )
                    except OSError as exc:
                        return f"failed to send Pi RPC stream recovery prompt: {exc}"

                    stream_recovery_state = "recovery_prompt_sent"
                    stream_recovery_deadline = None
                    stream_recovery_settled = False
                    assistant_stream_active = False
                    active_tool_calls.clear()
                    provider_error = ""
                    provider_failure_deadline = None
                    settled = False
                    agent_end_seen = False
                    agent_settled_seen = False
                    last_progress_at = time.monotonic()
                    last_event_type = "stream_recovery_prompt_sent"
                    _write_rpc_diagnostic(
                        stderr_log,
                        _rpc_stream_recovery_diagnostic(
                            event="pi_rpc_stream_recovery_resumed",
                            task_id=spec.environment.get("AXIOM_TASK_ID", ""),
                            intent_id=spec.environment.get("AXIOM_INTENT_ID", ""),
                            lease_id=spec.environment.get("AXIOM_LEASE_ID", ""),
                            idle_seconds=0.0,
                            last_event_type=last_event_type,
                            last_tool_name=last_tool_name,
                        ),
                    )
                    return ""

                while not protocol_error and not completed:
                    if should_cancel is not None:
                        cancel_reason = should_cancel()
                        if cancel_reason:
                            cancelled = True
                            break

                    now = time.monotonic()
                    if (
                        provider_failure_deadline is not None
                        and now >= provider_failure_deadline
                    ):
                        protocol_error = (
                            "Pi RPC provider failed and did not settle within "
                            f"{spec.provider_failure_grace_seconds:g}s: {provider_error}"
                        )
                        break
                    if now >= deadline:
                        timed_out = True
                        break
                    if (
                        stream_recovery_deadline is not None
                        and now >= stream_recovery_deadline
                    ):
                        rpc_stream_stalled = True
                        protocol_error = _rpc_stream_stall_diagnostic(
                            task_id=spec.environment.get("AXIOM_TASK_ID", ""),
                            intent_id=spec.environment.get("AXIOM_INTENT_ID", ""),
                            lease_id=spec.environment.get("AXIOM_LEASE_ID", ""),
                            idle_seconds=max(0.0, now - last_progress_at),
                            last_event_type=last_event_type,
                            last_tool_name=last_tool_name,
                            last_tool_started_at=last_tool_started_at,
                            last_tool_finished_at=last_tool_finished_at,
                            soft_reminder_sent=steer_attempted,
                            soft_reminder_acknowledged=soft_reminder_acknowledged,
                            agent_end_seen=agent_end_seen,
                            agent_settled_seen=agent_settled_seen,
                            recovery_state=stream_recovery_state,
                            recovery_abort_acknowledged=(
                                stream_recovery_abort_acknowledged
                            ),
                        )
                        break
                    if (
                        spec.stream_idle_timeout_seconds is not None
                        and process.poll() is None
                        and assistant_stream_active
                        and not active_tool_calls
                        and stream_recovery_deadline is None
                        and now - last_progress_at >= spec.stream_idle_timeout_seconds
                    ):
                        idle_seconds = now - last_progress_at
                        if spec.stream_recovery_prompt and not stream_recovery_attempted:
                            stream_recovery_attempted = True
                            stream_recovery_state = "abort_sent"
                            stream_recovery_abort_acknowledged = False
                            stream_recovery_settled = False
                            stream_recovery_deadline = min(
                                deadline,
                                now + spec.stream_recovery_abort_timeout_seconds,
                            )
                            _write_rpc_diagnostic(
                                stderr_log,
                                _rpc_stream_recovery_diagnostic(
                                    event="pi_rpc_stream_recovery_started",
                                    task_id=spec.environment.get("AXIOM_TASK_ID", ""),
                                    intent_id=spec.environment.get("AXIOM_INTENT_ID", ""),
                                    lease_id=spec.environment.get("AXIOM_LEASE_ID", ""),
                                    idle_seconds=idle_seconds,
                                    last_event_type=last_event_type,
                                    last_tool_name=last_tool_name,
                                ),
                            )
                            try:
                                _send_rpc(
                                    process,
                                    {
                                        "id": _RPC_STREAM_RECOVERY_ABORT_ID,
                                        "type": "abort",
                                    },
                                )
                            except OSError as exc:
                                rpc_stream_stalled = True
                                protocol_error = (
                                    "failed to abort stalled Pi RPC provider turn: "
                                    f"{exc}"
                                )
                                break
                            continue

                        rpc_stream_stalled = True
                        protocol_error = _rpc_stream_stall_diagnostic(
                            task_id=spec.environment.get("AXIOM_TASK_ID", ""),
                            intent_id=spec.environment.get("AXIOM_INTENT_ID", ""),
                            lease_id=spec.environment.get("AXIOM_LEASE_ID", ""),
                            idle_seconds=idle_seconds,
                            last_event_type=last_event_type,
                            last_tool_name=last_tool_name,
                            last_tool_started_at=last_tool_started_at,
                            last_tool_finished_at=last_tool_finished_at,
                            soft_reminder_sent=steer_attempted,
                            soft_reminder_acknowledged=soft_reminder_acknowledged,
                            agent_end_seen=agent_end_seen,
                            agent_settled_seen=agent_settled_seen,
                            recovery_state=stream_recovery_state,
                            recovery_abort_acknowledged=(
                                stream_recovery_abort_acknowledged
                            ),
                        )
                        break

                    if (
                        not steer_attempted
                        and steer_deadline is not None
                        and now >= steer_deadline
                        and process.poll() is None
                    ):
                        steer_attempted = True
                        try:
                            _send_rpc(
                                process,
                                {
                                    "id": _RPC_STEER_ID,
                                    "type": "steer",
                                    "message": spec.steer_message,
                                },
                            )
                        except OSError as exc:
                            logger.warning("failed to send Pi RPC soft reminder: %s", exc)

                    if (
                        spec.intervention_poller is not None
                        and now >= next_intervention_poll
                        and process.poll() is None
                        and stream_recovery_deadline is None
                    ):
                        next_intervention_poll = now + max(0.2, spec.intervention_poll_interval_seconds)
                        try:
                            interventions = spec.intervention_poller() or []
                        except Exception:
                            logger.warning("failed to poll human Explore interventions", exc_info=True)
                            interventions = []
                        for intervention_id, message in interventions:
                            rpc_id = f"{_RPC_INTERVENTION_PREFIX}{intervention_id}"
                            try:
                                _send_rpc(process, {"id": rpc_id, "type": "steer", "message": message})
                                intervention_commands[rpc_id] = intervention_id
                            except OSError as exc:
                                logger.warning("failed to send human Explore intervention: %s", exc)
                                if spec.intervention_result_reporter is not None:
                                    spec.intervention_result_reporter(intervention_id, False, str(exc))

                    wait_until = deadline
                    if not steer_attempted and steer_deadline is not None:
                        wait_until = min(wait_until, steer_deadline)
                    if provider_failure_deadline is not None:
                        wait_until = min(wait_until, provider_failure_deadline)
                    if stream_recovery_deadline is not None:
                        wait_until = min(wait_until, stream_recovery_deadline)
                    wait_seconds = max(
                        0.01,
                        min(spec.poll_interval_seconds, wait_until - time.monotonic()),
                    )
                    try:
                        event = events.get(timeout=wait_seconds)
                    except queue.Empty:
                        if process.poll() is not None and reader_done:
                            break
                        continue

                    if event is None:
                        reader_done = True
                        break

                    event_type = event.get("type")
                    event_id = event.get("id")
                    if event_type == "response":
                        success = event.get("success") is True
                        if event_id == _RPC_PROMPT_ID and not success:
                            protocol_error = str(
                                event.get("error") or "Pi RPC prompt command failed"
                            )
                            break
                        if event_id == _RPC_STREAM_RECOVERY_ABORT_ID:
                            if not success:
                                rpc_stream_stalled = True
                                protocol_error = str(
                                    event.get("error")
                                    or "Pi RPC stream recovery abort failed"
                                )
                                break
                            stream_recovery_abort_acknowledged = True
                            stream_recovery_state = "abort_acknowledged"
                            recovery_error = resume_after_stream_abort()
                            if recovery_error:
                                rpc_stream_stalled = True
                                protocol_error = recovery_error
                                break
                        if event_id == _RPC_STREAM_RECOVERY_PROMPT_ID and not success:
                            rpc_stream_stalled = True
                            protocol_error = (
                                "Pi RPC stream recovery prompt failed: "
                                + str(event.get("error") or "prompt command failed")
                            )
                            break
                        if event_id == _RPC_STEER_ID:
                            if not success:
                                logger.warning(
                                    "Pi RPC soft reminder was rejected: %s",
                                    str(event.get("error") or "Pi RPC steer command failed"),
                                )
                            else:
                                soft_reminder_acknowledged = True
                                if on_steer is not None:
                                    try:
                                        on_steer()
                                    except Exception:
                                        logger.warning(
                                            "failed to report Pi RPC soft reminder activity",
                                            exc_info=True,
                                        )
                        if event_id in intervention_commands:
                            intervention_id = intervention_commands.pop(event_id)
                            if spec.intervention_result_reporter is not None:
                                try:
                                    spec.intervention_result_reporter(
                                        intervention_id,
                                        success,
                                        "" if success else str(event.get("error") or "Pi RPC steer rejected"),
                                    )
                                except Exception:
                                    logger.warning("failed to persist human intervention result", exc_info=True)
                        if event_id == _RPC_FINAL_TEXT_ID:
                            if not final_text_requested:
                                protocol_error = (
                                    "Pi RPC returned final assistant text before agent_settled"
                                )
                                break
                            if not success:
                                protocol_error = str(
                                    event.get("error")
                                    or "Pi RPC final assistant text query failed"
                                )
                                break
                            data = event.get("data")
                            if not isinstance(data, dict) or "text" not in data:
                                protocol_error = (
                                    "Pi RPC final assistant text response is malformed"
                                )
                                break
                            text = data["text"]
                            if text is None:
                                last_assistant_text = ""
                            elif isinstance(text, str):
                                last_assistant_text = text
                            else:
                                protocol_error = (
                                    "Pi RPC final assistant text response is malformed"
                                )
                                break
                            completed = True
                            continue

                    if event_type == "_progress":
                        progress_type = str(event.get("event_type") or "")
                        last_progress_at = time.monotonic()
                        last_event_type = progress_type
                        message_role = str(event.get("message_role") or "")
                        tool_name = str(event.get("tool_name") or "")
                        tool_id = str(event.get("tool_id") or tool_name or "unknown")
                        if tool_name:
                            last_tool_name = tool_name
                        if progress_type == "message_start" and message_role == "assistant":
                            assistant_stream_active = True
                        elif progress_type == "message_update" and (
                            message_role == "assistant" or assistant_stream_active
                        ):
                            assistant_stream_active = True
                        elif progress_type == "message_end" and (
                            message_role == "assistant" or assistant_stream_active
                        ):
                            assistant_stream_active = False
                        elif progress_type in {
                            "tool_execution_start",
                            "bash_execution_start",
                        }:
                            if spec.external_action_reporter is not None:
                                try:
                                    spec.external_action_reporter()
                                except Exception:
                                    logger.warning(
                                        "failed to report external worker action",
                                        exc_info=True,
                                    )
                            active_tool_calls.add(tool_id)
                            assistant_stream_active = False
                            last_tool_started_at = last_progress_at
                        elif progress_type in {
                            "tool_execution_end",
                            "bash_execution_end",
                        }:
                            active_tool_calls.discard(tool_id)
                            if tool_id == "unknown" and len(active_tool_calls) == 1:
                                active_tool_calls.clear()
                            last_tool_finished_at = last_progress_at
                        continue

                    if event_type == "agent_end":
                        last_progress_at = time.monotonic()
                        last_event_type = event_type
                        agent_end_seen = True
                        assistant_stream_active = False
                        if stream_recovery_deadline is not None:
                            continue
                        terminal_error = _rpc_terminal_agent_error(event)
                        if terminal_error:
                            provider_error = terminal_error
                            provider_failure_deadline = min(
                                deadline,
                                time.monotonic() + spec.provider_failure_grace_seconds,
                            )
                        continue

                    if event_type == "auto_retry_start":
                        last_progress_at = time.monotonic()
                        last_event_type = event_type
                        assistant_stream_active = False
                        provider_error = ""
                        provider_failure_deadline = None
                        continue

                    if event_type == "auto_retry_end":
                        last_progress_at = time.monotonic()
                        last_event_type = event_type
                        assistant_stream_active = False
                        if stream_recovery_deadline is not None:
                            continue
                        if event.get("success") is True:
                            provider_error = ""
                            provider_failure_deadline = None
                        else:
                            provider_error = str(
                                event.get("finalError")
                                or provider_error
                                or "Pi provider retries were exhausted"
                            )
                            provider_failure_deadline = min(
                                deadline,
                                time.monotonic() + spec.provider_failure_grace_seconds,
                            )
                        continue

                    # ``agent_end`` is deliberately not a completion boundary.
                    # Pi may still retry, compact, or process queued messages.
                    if event_type == "agent_settled" and not final_text_requested:
                        last_progress_at = time.monotonic()
                        last_event_type = event_type
                        agent_settled_seen = True
                        settled = True
                        assistant_stream_active = False
                        if stream_recovery_deadline is not None:
                            stream_recovery_settled = True
                            if not stream_recovery_abort_acknowledged:
                                stream_recovery_state = "agent_settled"
                            recovery_error = resume_after_stream_abort()
                            if recovery_error:
                                rpc_stream_stalled = True
                                protocol_error = recovery_error
                            continue
                        if provider_error:
                            protocol_error = f"Pi RPC provider failed: {provider_error}"
                            break
                        try:
                            _send_rpc(
                                process,
                                {
                                    "id": _RPC_FINAL_TEXT_ID,
                                    "type": "get_last_assistant_text",
                                },
                            )
                        except OSError as exc:
                            protocol_error = (
                                f"failed to query Pi RPC final assistant text: {exc}"
                            )
                            break
                        final_text_requested = True

                if reader_done and not completed and not timed_out and not cancelled and not protocol_error:
                    if provider_error:
                        protocol_error = f"Pi RPC provider failed: {provider_error}"
                    else:
                        protocol_error = (
                            "Pi RPC stream ended before agent_settled"
                            if not settled
                            else "Pi RPC stream ended before final assistant text response"
                        )

                if timed_out or cancelled or protocol_error:
                    try:
                        _send_rpc(process, {"id": _RPC_ABORT_ID, "type": "abort"})
                    except OSError:
                        pass
                if protocol_error:
                    stderr_log.write((protocol_error + "\n").encode("utf-8", errors="replace"))
                    stderr_log.flush()

                _close_stdin(process)
                if process.poll() is None:
                    try:
                        process.wait(
                            timeout=(
                                spec.terminate_grace_seconds
                                if completed
                                else min(spec.terminate_grace_seconds, 0.5)
                            )
                        )
                    except subprocess.TimeoutExpired:
                        self._terminate(process, spec.terminate_grace_seconds)
                try:
                    returncode = process.wait(timeout=spec.terminate_grace_seconds + 1)
                except subprocess.TimeoutExpired:
                    self._terminate(process, spec.terminate_grace_seconds)
                    returncode = process.wait(timeout=spec.terminate_grace_seconds + 1)
                reader.join(timeout=spec.terminate_grace_seconds + 1)
        except OSError as exc:
            raise ProcessLaunchError(f"failed to start worker process: {spec.command[0]}") from exc
        finally:
            if process is not None:
                _close_stdin(process)
                if process.poll() is None:
                    self._terminate(process, spec.terminate_grace_seconds)
            if reader is not None and reader.is_alive():
                reader.join(timeout=spec.terminate_grace_seconds + 1)

        if timed_out and returncode == 0:
            returncode = 124
        elif protocol_error and returncode == 0:
            returncode = 1
        return ProcessResult(
            returncode=returncode,
            stdout=_read_tail(stdout_path, self.output_limit_bytes),
            stderr=_read_tail(stderr_path, self.output_limit_bytes),
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            timed_out=timed_out,
            cancelled=cancelled,
            cancel_reason=cancel_reason,
            last_assistant_text=last_assistant_text,
            rpc_stream_stalled=rpc_stream_stalled,
        )

    def _validate_rpc(self, spec: RpcProcessSpec) -> None:
        self._validate(spec)
        if not spec.prompt.strip():
            raise ValueError("RPC prompt must not be empty")
        if bool(spec.steer_message) != (spec.steer_after_seconds is not None):
            raise ValueError("RPC steer timing and message must be configured together")
        if spec.steer_after_seconds is not None and not (
            0 < spec.steer_after_seconds < spec.timeout_seconds
        ):
            raise ValueError("RPC steer timing must fall within the process timeout")
        if spec.stream_idle_timeout_seconds is not None and spec.stream_idle_timeout_seconds <= 0:
            raise ValueError("RPC stream idle timeout must be positive")
        if spec.stream_recovery_prompt is not None:
            if spec.stream_idle_timeout_seconds is None:
                raise ValueError("RPC stream recovery requires an idle timeout")
            if not spec.stream_recovery_prompt.strip():
                raise ValueError("RPC stream recovery prompt must not be empty")
        if spec.stream_recovery_abort_timeout_seconds <= 0:
            raise ValueError("RPC stream recovery abort timeout must be positive")
        if spec.provider_failure_grace_seconds <= 0:
            raise ValueError("RPC provider failure grace must be positive")


def _read_rpc_lines(
    stream,
    log,
    events: queue.Queue[dict[str, Any] | None],
) -> None:
    omitted: dict[str, int] = {}
    try:
        while True:
            line = stream.readline()
            if not line:
                break
            event: dict[str, Any] | None = None
            try:
                value = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                value = None
            if isinstance(value, dict):
                event = value

            event_type = str(event.get("type") or "") if event is not None else ""
            if event_type in _RPC_OMITTED_LOG_EVENT_TYPES:
                omitted[event_type] = omitted.get(event_type, 0) + 1
            else:
                log.write(line)
                log.flush()

            if event is not None and event_type in _RPC_CONTROL_EVENT_TYPES:
                events.put(_compact_rpc_control_event(event))
            elif event is not None and event_type in _RPC_PROGRESS_EVENT_TYPES:
                events.put(_compact_rpc_progress_event(event))
    finally:
        if omitted:
            summary = {
                "type": "axiom_rpc_log_summary",
                "omitted_events": dict(sorted(omitted.items())),
            }
            log.write(
                (json.dumps(summary, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
                    "utf-8"
                )
            )
            log.flush()
        events.put(None)


def _compact_rpc_control_event(event: dict[str, Any]) -> dict[str, Any]:
    event_type = str(event.get("type") or "")
    if event_type == "agent_end":
        return {
            "type": event_type,
            "willRetry": event.get("willRetry") is True,
            "error": _rpc_agent_error(event),
        }
    if event_type == "auto_retry_start":
        return {
            "type": event_type,
            "attempt": event.get("attempt"),
            "maxAttempts": event.get("maxAttempts"),
            "delayMs": event.get("delayMs"),
            "errorMessage": event.get("errorMessage"),
        }
    if event_type == "auto_retry_end":
        return {
            "type": event_type,
            "success": event.get("success") is True,
            "attempt": event.get("attempt"),
            "finalError": event.get("finalError"),
        }
    return event


def _compact_rpc_progress_event(event: dict[str, Any]) -> dict[str, Any]:
    """Return only the metadata needed by the semantic stream watchdog."""

    return {
        "type": "_progress",
        "event_type": str(event.get("type") or ""),
        "message_role": _rpc_event_message_role(event),
        "tool_name": _rpc_event_tool_name(event),
        "tool_id": _rpc_event_tool_id(event),
    }


def _rpc_event_message_role(event: dict[str, Any]) -> str:
    role = event.get("role")
    if isinstance(role, str) and role.strip():
        return role.strip()[:50]
    message = event.get("message")
    if isinstance(message, dict):
        role = message.get("role")
        if isinstance(role, str) and role.strip():
            return role.strip()[:50]
    return ""


def _rpc_event_tool_id(event: dict[str, Any]) -> str:
    for key in ("toolCallId", "tool_call_id", "id"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:200]
    for key in ("toolCall", "tool_call"):
        nested = event.get(key)
        if isinstance(nested, dict):
            tool_id = _rpc_event_tool_id(nested)
            if tool_id:
                return tool_id
    return _rpc_event_tool_name(event)


def _rpc_event_tool_name(event: dict[str, Any]) -> str:
    for key in ("toolName", "tool_name", "tool", "name", "toolCallId", "tool_call_id"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:200]
    for key in ("toolCall", "tool_call"):
        nested = event.get(key)
        if isinstance(nested, dict):
            name = _rpc_event_tool_name(nested)
            if name:
                return name
    return ""


def _rpc_stream_stall_diagnostic(
    *,
    task_id: str,
    intent_id: str,
    lease_id: str,
    idle_seconds: float,
    last_event_type: str,
    last_tool_name: str,
    last_tool_started_at: float | None,
    last_tool_finished_at: float | None,
    soft_reminder_sent: bool,
    soft_reminder_acknowledged: bool,
    agent_end_seen: bool,
    agent_settled_seen: bool,
    recovery_state: str,
    recovery_abort_acknowledged: bool,
) -> str:
    now = time.monotonic()

    def age(value: float | None) -> str:
        return "" if value is None else f"{max(0.0, now - value):.3f}"

    return "\n".join(
        (
            "pi_rpc_stream_stalled",
            f"task_id={task_id or 'unknown'}",
            f"intent_id={intent_id or 'unknown'}",
            f"lease_id={lease_id or 'unknown'}",
            f"idle_seconds={idle_seconds:.3f}",
            f"last_event_type={last_event_type or 'none'}",
            f"last_tool={last_tool_name or 'none'}",
            f"last_tool_started_ago_seconds={age(last_tool_started_at) or 'none'}",
            f"last_tool_finished_ago_seconds={age(last_tool_finished_at) or 'none'}",
            f"soft_reminder_sent={'true' if soft_reminder_sent else 'false'}",
            f"soft_reminder_acknowledged={'true' if soft_reminder_acknowledged else 'false'}",
            f"agent_end_seen={'true' if agent_end_seen else 'false'}",
            f"agent_settled_seen={'true' if agent_settled_seen else 'false'}",
            f"recovery_state={recovery_state or 'none'}",
            (
                "recovery_abort_acknowledged="
                f"{'true' if recovery_abort_acknowledged else 'false'}"
            ),
        )
    )


def _rpc_stream_recovery_diagnostic(
    *,
    event: str,
    task_id: str,
    intent_id: str,
    lease_id: str,
    idle_seconds: float,
    last_event_type: str,
    last_tool_name: str,
) -> str:
    return "\n".join(
        (
            event,
            f"task_id={task_id or 'unknown'}",
            f"intent_id={intent_id or 'unknown'}",
            f"lease_id={lease_id or 'unknown'}",
            f"idle_seconds={idle_seconds:.3f}",
            f"last_event_type={last_event_type or 'none'}",
            f"last_tool={last_tool_name or 'none'}",
        )
    )


def _write_rpc_diagnostic(log, diagnostic: str) -> None:
    log.write((diagnostic + "\n").encode("utf-8", errors="replace"))
    log.flush()


def _rpc_terminal_agent_error(event: dict[str, Any]) -> str:
    if event.get("willRetry") is True:
        return ""
    return str(event.get("error") or "")


def _rpc_agent_error(event: dict[str, Any]) -> str:
    messages = event.get("messages")
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        if message.get("stopReason") != "error":
            return ""
        error = message.get("errorMessage")
        if isinstance(error, str) and error.strip():
            return error.strip()
        content = message.get("content")
        if isinstance(content, list):
            text = "\n".join(
                str(item.get("text"))
                for item in content
                if isinstance(item, dict)
                and item.get("type") == "text"
                and item.get("text")
            ).strip()
            if text:
                return text
        return "Pi provider returned an error response"
    return ""


def _send_rpc(process: subprocess.Popen, payload: dict[str, Any]) -> None:
    assert process.stdin is not None
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    process.stdin.write(line.encode("utf-8"))
    process.stdin.flush()


def _close_stdin(process: subprocess.Popen) -> None:
    if process.stdin is None or process.stdin.closed:
        return
    try:
        process.stdin.close()
    except OSError:
        pass


def _read_tail(path: Path, limit: int) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > limit:
                handle.seek(-limit, os.SEEK_END)
            payload = handle.read(limit)
        return payload.decode("utf-8", errors="replace")
    except OSError:
        return ""
