from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path


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
