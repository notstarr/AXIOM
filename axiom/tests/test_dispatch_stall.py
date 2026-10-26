"""Stalled-Task observation (``AxiomRunner._observe_stalls``).

A Task that stops making progress emits no events, so it can only be detected by
counting ticks that produced no claim.  This is the last-resort visibility layer:
it runs *after* automatic recovery has had its chance, and it never changes
scheduling state — a false positive must cost one audit line and nothing more.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from axiom.dispatcher.runner import AxiomRunner
from axiom.dispatcher.schemas import (
    DispatchLimits,
    DispatcherSnapshot,
    IntentSnapshot,
    TaskSnapshot,
    WorkerSnapshot,
)


NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)


class _ControlPlane:
    """Minimal control plane: one snapshot, plus a stall sink."""

    def __init__(self, snapshot: DispatcherSnapshot):
        self.current = snapshot
        self.stalls: list[dict] = []
        self.fail_stalls = False

    def snapshot(self) -> DispatcherSnapshot:
        return self.current

    def claim(self, *, task_id, intent_id, worker_name):
        return None

    def report_dispatch_stall(self, *, task_id, idle_seconds, ready_count, parked_count):
        if self.fail_stalls:
            raise RuntimeError("control plane down")
        self.stalls.append(
            {
                "task_id": task_id,
                "idle_seconds": idle_seconds,
                "ready_count": ready_count,
                "parked_count": parked_count,
            }
        )


class _Config:
    infrastructure_retry_limit = 3
    dispatch_stall_seconds = 30


def _runner(control_plane, *, interval: float = 3.0) -> AxiomRunner:
    runner = AxiomRunner(control_plane, max_parallel=4, heartbeat_interval_seconds=10)
    runner.runner_interval_seconds = interval
    runner._last_runtime_config = _Config()
    return runner


def _task(*, intents=None, reason_runs=None) -> TaskSnapshot:
    return TaskSnapshot(
        id="t_001",
        status="running",
        intents=list(intents or []),
        reason_runs=list(reason_runs or []),
    )


class _Report:
    """A dispatch report for one tick: nothing claimed unless stated."""

    def __init__(self, claimed: tuple[str, ...] = ()):
        self.claims = [type("Work", (), {"task_id": task_id})() for task_id in claimed]


def _snapshot(task: TaskSnapshot, *, worker_capabilities=("explore",)) -> DispatcherSnapshot:
    return DispatcherSnapshot(
        tasks=[task],
        workers=[
            WorkerSnapshot(
                name="worker-a",
                capabilities=frozenset(worker_capabilities),
                max_running=2,
                running=0,
            )
        ],
        limits=DispatchLimits(infrastructure_retry_limit=3),
    )


def _parked_intent(number: int) -> IntentSnapshot:
    return IntentSnapshot(
        task_id="t_001",
        id=f"i{number:03d}",
        kind="explore",
        status="pending",
        infrastructure_retry_count=3,
        created_at=NOW + timedelta(seconds=number),
    )


def test_a_task_with_only_parked_work_reports_a_stall_once():
    control = _ControlPlane(_snapshot(_task(intents=[_parked_intent(1)])))
    runner = _runner(control)

    for _ in range(10):
        runner._observe_stalls(control.snapshot(), _Report())

    assert len(control.stalls) == 1
    assert control.stalls[0]["parked_count"] == 1
    assert control.stalls[0]["ready_count"] == 0


def test_the_threshold_is_respected_before_reporting():
    control = _ControlPlane(_snapshot(_task(intents=[_parked_intent(1)])))
    runner = _runner(control, interval=3.0)  # 30s threshold → 10 ticks

    for _ in range(9):
        runner._observe_stalls(control.snapshot(), _Report())
    assert control.stalls == []

    runner._observe_stalls(control.snapshot(), _Report())
    assert len(control.stalls) == 1


def test_a_claim_clears_the_idle_counter_and_allows_a_later_Report():
    control = _ControlPlane(_snapshot(_task(intents=[_parked_intent(1)])))
    runner = _runner(control, interval=3.0)
    for _ in range(10):
        runner._observe_stalls(control.snapshot(), _Report())
    assert len(control.stalls) == 1

    runner._observe_stalls(control.snapshot(), _Report(claimed=("t_001",)))
    assert runner._idle_ticks.get("t_001") is None

    for _ in range(10):
        runner._observe_stalls(control.snapshot(), _Report())
    assert len(control.stalls) == 2


def test_a_task_with_active_work_is_not_stalled():
    busy = IntentSnapshot(
        task_id="t_001",
        id="i001",
        kind="explore",
        status="running",
        created_at=NOW,
    )
    control = _ControlPlane(_snapshot(_task(intents=[busy])))
    runner = _runner(control, interval=3.0)

    for _ in range(20):
        runner._observe_stalls(control.snapshot(), _Report())

    assert control.stalls == []


def test_a_task_with_nothing_pending_is_not_stalled():
    control = _ControlPlane(_snapshot(_task()))
    runner = _runner(control, interval=3.0)

    for _ in range(20):
        runner._observe_stalls(control.snapshot(), _Report())

    assert control.stalls == []


def test_a_stopped_task_is_never_reported():
    task = _task(intents=[_parked_intent(1)])
    task.status = "paused"
    control = _ControlPlane(_snapshot(task))
    runner = _runner(control, interval=3.0)

    for _ in range(20):
        runner._observe_stalls(control.snapshot(), _Report())

    assert control.stalls == []


def test_a_reporting_failure_does_not_break_the_loop_and_is_retried():
    control = _ControlPlane(_snapshot(_task(intents=[_parked_intent(1)])))
    control.fail_stalls = True
    runner = _runner(control, interval=3.0)

    for _ in range(10):
        runner._observe_stalls(control.snapshot(), _Report())
    assert control.stalls == []

    control.fail_stalls = False
    runner._observe_stalls(control.snapshot(), _Report())
    assert len(control.stalls) == 1
