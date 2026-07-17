from __future__ import annotations

from datetime import datetime, timezone

from axiom.dispatcher.v2.reconciler import Reconciler
from axiom.dispatcher.v2.retry import BootstrapRetryPolicy, ExploreRetryPolicy
from axiom.dispatcher.v2.schemas import IntentSnapshot, TaskSnapshot, WorkerSnapshot
from axiom.dispatcher.v2.worker_allocator import WorkerAllocator


def task(**overrides) -> TaskSnapshot:
    values = {
        "id": "t_001",
        "status": "running",
        "phase": "bootstrap",
        "reason_worker": None,
        "reason_due": True,
        "worker_pool": [],
        "intents": [
            IntentSnapshot(
                id="i001",
                status="pending",
                creator="dispatcher.bootstrap",
                retry_count=0,
            )
        ],
    }
    values.update(overrides)
    return TaskSnapshot(**values)


def test_reconciler_calculates_only_desired_phase_actions() -> None:
    reconciler = Reconciler()
    actions = reconciler.calculate([task()])
    assert [(item.kind, item.task_id, item.intent_id) for item in actions] == [
        ("bootstrap", "t_001", "i001")
    ]

    reason_task = task(phase="reason", intents=[])
    assert reconciler.calculate([reason_task])[0].kind == "reason"

    explore_task = task(
        phase="explore",
        intents=[
            IntentSnapshot(id="i002", status="pending", creator="reasoner", retry_count=0),
            IntentSnapshot(id="i003", status="running", creator="reasoner", retry_count=0),
        ],
    )
    actions = reconciler.calculate([explore_task])
    assert [(item.kind, item.intent_id) for item in actions] == [("explore", "i002")]

    no_open = task(phase="explore", intents=[])
    assert reconciler.calculate([no_open])[0].kind == "reason"


def test_reconciler_skips_stopped_claimed_and_backoff_work() -> None:
    reconciler = Reconciler(now=lambda: datetime(2026, 7, 17, tzinfo=timezone.utc))
    assert reconciler.calculate([task(status="stopped")]) == []
    assert reconciler.calculate([task(phase="reason", reason_worker="reason-a")]) == []
    blocked = task(
        intents=[
            IntentSnapshot(
                id="i001",
                status="pending",
                creator="dispatcher.bootstrap",
                retry_count=1,
                next_retry_at="2026-07-17T00:01:00Z",
            )
        ]
    )
    assert reconciler.calculate([blocked]) == []


def test_retry_policies_preserve_existing_behavior() -> None:
    explore = ExploreRetryPolicy()
    assert [explore.execute_timeout(240, retry) for retry in range(4)] == [240, 480, 960, 960]
    assert [explore.conclude_timeout(90, retry) for retry in range(4)] == [90, 180, 270, 270]
    assert explore.can_attempt(3) is True
    assert explore.can_attempt(4) is False

    bootstrap = BootstrapRetryPolicy()
    first = bootstrap.after_rejection(previous_count=0, signature="s1", now_epoch=100)
    third = bootstrap.after_rejection(previous_count=2, signature="s1", now_epoch=100)
    assert first.retry_count == 1 and first.next_retry_epoch == 130 and first.needs_hint is False
    assert third.retry_count == 3 and third.needs_hint is True


def test_worker_allocator_filters_capability_pool_capacity_and_cooldown() -> None:
    workers = [
        WorkerSnapshot(
            name="busy",
            capabilities=["explore"],
            max_running=1,
            running=1,
            priority=0,
        ),
        WorkerSnapshot(
            name="reason-only",
            capabilities=["reason"],
            max_running=2,
            running=0,
            priority=0,
        ),
        WorkerSnapshot(
            name="available",
            capabilities=["explore"],
            max_running=2,
            running=1,
            priority=1,
        ),
    ]
    selected = WorkerAllocator().choose(
        workers,
        capability="explore",
        task_pool=["busy", "available"],
    )
    assert selected is not None
    assert selected.name == "available"
