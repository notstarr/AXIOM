from __future__ import annotations

import threading
import time

from axiom.dispatcher.process import ProcessSpec, ProcessSupervisor


def test_process_supervisor_preserves_logs_and_returns_success(tmp_path) -> None:
    result = ProcessSupervisor().run(
        ProcessSpec(
            command=["/bin/sh", "-c", "printf out; printf err >&2"],
            log_dir=tmp_path / "logs",
            timeout_seconds=2,
        )
    )

    assert result.returncode == 0
    assert result.stdout == "out"
    assert result.stderr == "err"
    assert result.stdout_path.read_text() == "out"
    assert result.stderr_path.read_text() == "err"


def test_process_supervisor_terminates_a_process_group_on_timeout(tmp_path) -> None:
    started = time.monotonic()
    result = ProcessSupervisor().run(
        ProcessSpec(
            command=["/bin/sh", "-c", "sleep 30"],
            log_dir=tmp_path / "logs",
            timeout_seconds=0.1,
            terminate_grace_seconds=0.2,
            poll_interval_seconds=0.02,
        )
    )

    assert result.timed_out is True
    assert result.returncode != 0
    assert time.monotonic() - started < 3


def test_process_supervisor_cancels_when_lease_or_task_is_lost(tmp_path) -> None:
    cancelled = threading.Event()

    def should_cancel() -> str | None:
        return "task_stopped" if cancelled.is_set() else None

    def cancel_later() -> None:
        time.sleep(0.1)
        cancelled.set()

    thread = threading.Thread(target=cancel_later)
    thread.start()
    result = ProcessSupervisor().run(
        ProcessSpec(
            command=["/bin/sh", "-c", "sleep 30"],
            log_dir=tmp_path / "logs",
            timeout_seconds=5,
            terminate_grace_seconds=0.2,
            poll_interval_seconds=0.02,
        ),
        should_cancel=should_cancel,
    )
    thread.join()

    assert result.cancelled is True
    assert result.cancel_reason == "task_stopped"
    assert result.timed_out is False
