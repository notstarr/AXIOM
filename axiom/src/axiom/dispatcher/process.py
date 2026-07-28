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
_RPC_CONTROL_EVENT_TYPES = frozenset({"response", "agent_settled"})
_RPC_OMITTED_LOG_EVENT_TYPES = frozenset(
    {"message_update", "tool_execution_update", "bash_execution_update"}
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


@dataclass(frozen=True, slots=True)
class RpcProcessSpec(ProcessSpec):
    prompt: str = ""
    steer_after_seconds: float | None = None
    steer_message: str | None = None


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
        last_assistant_text: str | None = None
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
                deadline = started + spec.timeout_seconds
                steer_deadline = (
                    started + spec.steer_after_seconds
                    if spec.steer_after_seconds is not None
                    else None
                )
                steer_attempted = False

                while not protocol_error and not completed:
                    if should_cancel is not None:
                        cancel_reason = should_cancel()
                        if cancel_reason:
                            cancelled = True
                            break

                    now = time.monotonic()
                    if now >= deadline:
                        timed_out = True
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

                    wait_until = deadline
                    if not steer_attempted and steer_deadline is not None:
                        wait_until = min(wait_until, steer_deadline)
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
                        if event_id == _RPC_STEER_ID:
                            if not success:
                                logger.warning(
                                    "Pi RPC soft reminder was rejected: %s",
                                    str(event.get("error") or "Pi RPC steer command failed"),
                                )
                            elif on_steer is not None:
                                try:
                                    on_steer()
                                except Exception:
                                    logger.warning(
                                        "failed to report Pi RPC soft reminder activity",
                                        exc_info=True,
                                    )
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

                    # ``agent_end`` is deliberately not a completion boundary.
                    # Pi may still retry, compact, or process queued messages.
                    if event_type == "agent_settled" and not final_text_requested:
                        settled = True
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
                events.put(event)
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
