from __future__ import annotations

import json
import logging
import queue
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

LOG = logging.getLogger(__name__)
RPC_ABORT_GRACE_SECONDS = 10.0


@dataclass(slots=True)
class RpcProcessResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    soft_steered: bool = False
    soft_aborted: bool = False
    cancelled: bool = False
    cancel_reason: str | None = None


class ManagedRpcProcess:
    """A line-oriented PI RPC process with cooperative phase deadlines.

    The process stays alive between prompts, so a soft deadline can steer the
    current agent in the same session instead of killing it and replaying its
    entire history in a second process.
    """

    def __init__(self, command: list[str], env: dict[str, str]):
        self.command = command
        self.env = env
        self._process: subprocess.Popen[str] | None = None
        self._events: queue.Queue[tuple[str, str | None]] = queue.Queue()
        self._reader_threads: list[threading.Thread] = []
        self._stdout_lines: list[str] = []
        self._stderr_lines: list[str] = []
        self._lock = threading.Lock()
        self._cancel_reason: str | None = None
        self._event_callback: Callable[[dict[str, Any]], None] | None = None
        self._deadline_callback: Callable[[str, dict[str, Any]], None] | None = None

    def set_event_callback(self, callback: Callable[[dict[str, Any]], None] | None) -> None:
        self._event_callback = callback

    def set_deadline_callback(
        self,
        callback: Callable[[str, dict[str, Any]], None] | None,
    ) -> None:
        """Register a callback for deadline transitions.

        Deadline events are emitted from the watchdog loop at the moment the
        transition occurs.  Runners must not wait for the RPC turn to return
        before auditing them: a close-out turn may itself take long enough to
        obscure when the execute budget was actually reached.
        """
        self._deadline_callback = callback

    def start(self) -> None:
        import os

        merged_env = os.environ.copy()
        merged_env.update(self.env)
        self._process = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=merged_env,
            start_new_session=True,
            bufsize=1,
        )
        assert self._process.stdout is not None
        assert self._process.stderr is not None
        for stream_name, stream in (("stdout", self._process.stdout), ("stderr", self._process.stderr)):
            thread = threading.Thread(
                target=self._read_stream,
                args=(stream_name, stream),
                daemon=True,
            )
            self._reader_threads.append(thread)
            thread.start()

    def prompt(
        self,
        message: str,
        *,
        soft_timeout: float | None,
        hard_timeout: float | None,
        steering_message: str | None = None,
        abort_after_steer: float | None = None,
    ) -> RpcProcessResult:
        self._send({"type": "prompt", "message": message})
        return self._wait_for_agent_end(
            soft_timeout=soft_timeout,
            hard_timeout=hard_timeout,
            steering_message=steering_message,
            abort_after_steer=abort_after_steer,
        )

    def follow_up(
        self,
        message: str,
        *,
        hard_timeout: float | None,
    ) -> RpcProcessResult:
        # `_wait_for_agent_end` only returns after PI is idle. PI's
        # `follow_up` RPC command is a queueing primitive for a turn that is
        # still running; when sent after an aborted/finished turn it may remain
        # queued without starting another model turn. Use a normal prompt here
        # so the forced-conclusion pass always starts immediately in the same
        # session.
        self._send({"type": "prompt", "message": message})
        return self._wait_for_agent_end(soft_timeout=None, hard_timeout=hard_timeout)

    def stop(self) -> None:
        process = self._process
        if process is None:
            return
        self._terminate_process_group(signal.SIGTERM)
        try:
            process.wait(timeout=RPC_ABORT_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            self._terminate_process_group(signal.SIGKILL)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        for thread in self._reader_threads:
            thread.join(timeout=1)

    def cancel(self, reason: str) -> None:
        self._cancel_reason = reason
        try:
            self._send({"type": "abort"})
        except Exception:
            LOG.debug("failed to send RPC abort", exc_info=True)
        self.stop()

    def kill(self) -> None:
        self._terminate_process_group(signal.SIGKILL)

    def _wait_for_agent_end(
        self,
        *,
        soft_timeout: float | None,
        hard_timeout: float | None,
        steering_message: str | None = None,
        abort_after_steer: float | None = None,
    ) -> RpcProcessResult:
        started = time.monotonic()
        last_activity_at = started
        soft_sent = False
        soft_abort_sent = False
        hard_deadline = started + hard_timeout if hard_timeout is not None else None
        stdout_start = len(self._stdout_lines)
        stderr_start = len(self._stderr_lines)
        while True:
            now = time.monotonic()
            if not soft_sent and soft_timeout is not None and now - started >= soft_timeout:
                if steering_message:
                    self._send({"type": "steer", "message": steering_message})
                soft_sent = True
                self._emit_deadline(
                    "soft_deadline",
                    {
                        "elapsed_seconds": round(now - started, 3),
                        "soft_timeout_seconds": soft_timeout,
                    },
                )
                # Start a fresh inactivity window when steering. Activity
                # from before the soft deadline must not shorten the model's
                # opportunity to obey the steer request.
                last_activity_at = now
            if (
                soft_sent
                and not soft_abort_sent
                and abort_after_steer is not None
                and now - last_activity_at >= abort_after_steer
            ):
                self._send({"type": "abort"})
                soft_abort_sent = True
                self._emit_deadline(
                    "soft_abort",
                    {
                        "elapsed_seconds": round(now - started, 3),
                        "abort_after_steer_seconds": abort_after_steer,
                    },
                )
            if hard_deadline is not None and now >= hard_deadline:
                self._emit_deadline(
                    "hard_deadline",
                    {
                        "elapsed_seconds": round(now - started, 3),
                        "hard_timeout_seconds": hard_timeout,
                    },
                )
                return self._abort_after_hard_deadline(
                    stdout_start=stdout_start,
                    stderr_start=stderr_start,
                    soft_steered=soft_sent,
                    soft_aborted=soft_abort_sent,
                )
            try:
                stream, line = self._events.get(timeout=0.25)
            except queue.Empty:
                process = self._process
                if process is not None and process.poll() is not None:
                    return self._result(
                        stdout_start=stdout_start,
                        stderr_start=stderr_start,
                        soft_steered=soft_sent,
                        soft_aborted=soft_abort_sent,
                    )
                continue
            if line is None:
                continue
            if stream == "stdout":
                payload = self._json_payload(line)
                if payload is not None:
                    # The grace period after steer is an inactivity timeout,
                    # not a fixed wall-clock delay. PI may still be making
                    # useful progress (for example, writing an artifact) long
                    # after the soft deadline. Only abort a turn that has
                    # emitted no RPC event for the configured grace period.
                    last_activity_at = time.monotonic()
                    self._emit_event(payload)
                if self._is_agent_end(payload):
                    return self._result(
                        stdout_start=stdout_start,
                        stderr_start=stderr_start,
                        soft_steered=soft_sent,
                        soft_aborted=soft_abort_sent,
                    )

    def _abort_after_hard_deadline(
        self,
        *,
        stdout_start: int,
        stderr_start: int,
        soft_steered: bool,
        soft_aborted: bool = False,
    ) -> RpcProcessResult:
        try:
            self._send({"type": "abort"})
        except Exception:
            LOG.debug("failed to send RPC abort at hard deadline", exc_info=True)
        grace_deadline = time.monotonic() + RPC_ABORT_GRACE_SECONDS
        while time.monotonic() < grace_deadline:
            try:
                stream, line = self._events.get(timeout=0.25)
            except queue.Empty:
                process = self._process
                if process is not None and process.poll() is not None:
                    break
                continue
            if stream == "stdout" and line is not None:
                payload = self._json_payload(line)
                if payload is not None:
                    self._emit_event(payload)
                if self._is_agent_end(payload):
                    return self._result(
                        stdout_start=stdout_start,
                        stderr_start=stderr_start,
                        timed_out=True,
                        soft_steered=soft_steered,
                        soft_aborted=soft_aborted,
                    )
        self._terminate_process_group(signal.SIGKILL)
        return self._result(
            stdout_start=stdout_start,
            stderr_start=stderr_start,
            timed_out=True,
            soft_steered=soft_steered,
            soft_aborted=soft_aborted,
        )

    def _send(self, payload: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise RuntimeError("RPC process is not running")
        encoded = json.dumps(payload, ensure_ascii=False) + "\n"
        with self._lock:
            process.stdin.write(encoded)
            process.stdin.flush()

    def _read_stream(self, stream_name: str, stream) -> None:
        try:
            for line in iter(stream.readline, ""):
                line = line.rstrip("\n")
                if stream_name == "stdout":
                    self._stdout_lines.append(line)
                else:
                    self._stderr_lines.append(line)
                self._events.put((stream_name, line))
        finally:
            self._events.put((stream_name, None))

    def _result(
        self,
        *,
        stdout_start: int,
        stderr_start: int,
        timed_out: bool = False,
        soft_steered: bool = False,
        soft_aborted: bool = False,
    ) -> RpcProcessResult:
        process = self._process
        returncode = process.poll() if process is not None else 1
        return RpcProcessResult(
            returncode=0 if returncode is None else returncode,
            stdout="\n".join(self._stdout_lines[stdout_start:]),
            stderr="\n".join(self._stderr_lines[stderr_start:]),
            timed_out=timed_out,
            soft_steered=soft_steered,
            soft_aborted=soft_aborted,
            cancelled=self._cancel_reason is not None,
            cancel_reason=self._cancel_reason,
        )

    def _terminate_process_group(self, sig: signal.Signals) -> None:
        process = self._process
        if process is None or process.poll() is not None:
            return
        try:
            import os

            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return
        except Exception:
            try:
                if sig == signal.SIGKILL:
                    process.kill()
                else:
                    process.terminate()
            except Exception:
                LOG.debug("failed to terminate RPC process", exc_info=True)

    def _emit_event(self, payload: dict[str, Any]) -> None:
        callback = self._event_callback
        if callback is None:
            return
        try:
            callback(payload)
        except Exception:
            LOG.debug("RPC event callback failed", exc_info=True)

    def _emit_deadline(self, event: str, payload: dict[str, Any]) -> None:
        callback = self._deadline_callback
        if callback is None:
            return
        try:
            callback(event, payload)
        except Exception:
            LOG.debug("RPC deadline callback failed event=%s", event, exc_info=True)

    @staticmethod
    def _json_payload(line: str) -> dict[str, Any] | None:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _is_agent_end(payload: dict[str, Any] | None) -> bool:
        return bool(payload and payload.get("type") == "agent_end")
