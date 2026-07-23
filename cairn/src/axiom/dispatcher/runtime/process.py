from __future__ import annotations

from dataclasses import dataclass
import logging
import os
import signal
import subprocess
import threading
from collections.abc import Callable

LOG = logging.getLogger(__name__)
PROCESS_KILL_JOIN_TIMEOUT_SECONDS = 5.0
PROCESS_TIMEOUT_GRACE_SECONDS = 3.0


@dataclass(slots=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    cancelled: bool = False
    cancel_reason: str | None = None
    soft_steered: bool = False
    soft_aborted: bool = False


class ManagedProcess:
    def __init__(self, command: list[str], env: dict[str, str], *, cwd: str | None = None):
        self.command = command
        self.env = env
        self.cwd = cwd
        self._process: subprocess.Popen[str] | None = None
        self._stdout = ""
        self._stderr = ""
        self._returncode: int | None = None
        self._timed_out = False
        self._cancel_reason: str | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        merged_env = os.environ.copy()
        merged_env.update(self.env)
        self._process = subprocess.Popen(
            self.command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=self.cwd,
            env=merged_env,
            start_new_session=True,
        )

    def communicate(
        self,
        timeout: float | None,
        *,
        timeout_callback: Callable[[], None] | None = None,
    ) -> ProcessResult:
        assert self._process is not None
        try:
            stdout, stderr = self._process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._timed_out = True
            if timeout_callback is not None:
                try:
                    timeout_callback()
                except Exception:
                    LOG.debug("managed process timeout callback failed", exc_info=True)
            # A phase timeout is an execution-budget boundary, not a crash.
            # Give the agent a brief window to flush its persisted session.
            self._terminate_gracefully()
            try:
                stdout, stderr = self._process.communicate(timeout=PROCESS_TIMEOUT_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                self._kill_forcefully()
                try:
                    stdout, stderr = self._process.communicate(
                        timeout=PROCESS_KILL_JOIN_TIMEOUT_SECONDS
                    )
                except subprocess.TimeoutExpired:
                    stdout, stderr = "", ""
        self._stdout = stdout or ""
        self._stderr = stderr or ""
        self._returncode = self._process.returncode
        if self._returncode is None:
            self._returncode = 137 if self._timed_out else 1
        return ProcessResult(
            returncode=self._returncode,
            stdout=self._stdout,
            stderr=self._stderr,
            timed_out=self._timed_out,
            cancelled=self._cancel_reason is not None,
            cancel_reason=self._cancel_reason,
        )

    def kill(self) -> None:
        if self._process is None:
            return
        with self._lock:
            if self._process.poll() is not None:
                return
            pid = self._process.pid
            if pid is None:
                return
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                return
            except Exception as exc:
                LOG.warning("failed to kill process group pid=%s error=%s", pid, exc)
                try:
                    self._process.kill()
                except Exception:
                    pass

    def cancel(self, reason: str) -> None:
        if self._cancel_reason is None:
            self._cancel_reason = reason
        self.kill()

    def _terminate_gracefully(self) -> None:
        if self._process is None or self._process.poll() is not None:
            return
        try:
            os.killpg(self._process.pid, signal.SIGTERM)
        except Exception:
            try:
                self._process.terminate()
            except Exception:
                pass

    def _kill_forcefully(self) -> None:
        if self._process is None or self._process.poll() is not None:
            return
        try:
            os.killpg(self._process.pid, signal.SIGKILL)
        except Exception:
            try:
                self._process.kill()
            except Exception:
                pass

    def stop(self) -> None:
        self._terminate_gracefully()
        if self._process is None:
            return
        while True:
            try:
                self._process.wait(timeout=PROCESS_KILL_JOIN_TIMEOUT_SECONDS)
                return
            except subprocess.TimeoutExpired:
                self._kill_forcefully()
