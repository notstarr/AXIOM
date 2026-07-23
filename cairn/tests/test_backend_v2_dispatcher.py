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
        reason_due=False,
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


def test_reconciler_reopens_reason_for_waiting_task_with_unfinished_todos() -> None:
    waiting = task(
        phase="waiting",
        reason_due=True,
        pending_todo_count=3,
        intents=[],
    )
    actions = Reconciler().calculate([waiting])
    assert [(item.kind, item.task_id) for item in actions] == [("reason", "t_001")]


def test_reconciler_runs_reason_incrementally_without_stranding_pending_intents() -> None:
    incremental = task(
        phase="explore",
        reason_due=True,
        intents=[
            IntentSnapshot(id="i002", status="pending", creator="reasoner", retry_count=0),
            IntentSnapshot(id="i003", status="running", creator="reasoner", retry_count=0),
        ],
    )
    actions = Reconciler().calculate([incremental])
    assert [(item.kind, item.intent_id) for item in actions] == [
        ("explore", "i002"),
        ("reason", None),
    ]

    while_reason = task(
        phase="reason",
        reason_due=True,
        reason_worker="reasoner",
        intents=[
            IntentSnapshot(id="i002", status="pending", creator="reasoner", retry_count=0),
        ],
    )
    actions = Reconciler().calculate([while_reason])
    assert [(item.kind, item.intent_id) for item in actions] == [("explore", "i002")]


def test_reconciler_batches_terminal_results_before_incremental_reason() -> None:
    now = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
    reconciler = Reconciler(now=lambda: now)
    base = task(
        phase="explore",
        reason_due=True,
        last_reason_committed_at="2026-07-22T11:55:00Z",
        oldest_terminal_delta_at="2026-07-22T11:59:30Z",
        open_explore_intent_count=3,
        intents=[
            IntentSnapshot(id="i002", status="running", creator="reasoner"),
            IntentSnapshot(id="i003", status="running", creator="reasoner"),
            IntentSnapshot(id="i004", status="pending", creator="reasoner"),
        ],
    )

    one_result = base.model_copy(update={"terminal_delta_count": 1})
    assert [item.kind for item in reconciler.calculate([one_result])] == ["explore"]

    two_results = base.model_copy(update={"terminal_delta_count": 2})
    assert [item.kind for item in reconciler.calculate([two_results])] == [
        "explore",
        "reason",
    ]


def test_reconciler_reason_low_watermark_and_max_wait_prevent_starvation() -> None:
    now = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
    reconciler = Reconciler(now=lambda: now)
    low_queue = task(
        phase="explore",
        reason_due=True,
        terminal_delta_count=1,
        oldest_terminal_delta_at="2026-07-22T11:59:50Z",
        last_reason_committed_at="2026-07-22T11:55:00Z",
        open_explore_intent_count=1,
        intents=[IntentSnapshot(id="i002", status="running", creator="reasoner")],
    )
    assert [item.kind for item in reconciler.calculate([low_queue])] == ["reason"]

    waited = low_queue.model_copy(
        update={
            "open_explore_intent_count": 3,
            "oldest_terminal_delta_at": "2026-07-22T11:56:00Z",
            "intents": [
                IntentSnapshot(id="i002", status="running", creator="reasoner"),
                IntentSnapshot(id="i003", status="running", creator="reasoner"),
                IntentSnapshot(id="i004", status="running", creator="reasoner"),
            ],
        }
    )
    assert [item.kind for item in reconciler.calculate([waited])] == ["reason"]


def test_reconciler_reason_minimum_interval_blocks_back_to_back_rounds() -> None:
    now = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
    recent = task(
        phase="explore",
        reason_due=True,
        terminal_delta_count=2,
        oldest_terminal_delta_at="2026-07-22T11:59:50Z",
        last_reason_committed_at="2026-07-22T11:59:30Z",
        open_explore_intent_count=1,
        intents=[IntentSnapshot(id="i002", status="running", creator="reasoner")],
    )
    assert Reconciler(now=lambda: now).calculate([recent]) == []


def test_reconciler_replans_retry_exhaustion_without_waiting_for_batch() -> None:
    now = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
    replan = task(
        phase="explore",
        reason_due=True,
        terminal_delta_count=1,
        needs_replan_due=True,
        oldest_terminal_delta_at="2026-07-22T11:59:55Z",
        last_reason_committed_at="2026-07-22T11:55:00Z",
        open_explore_intent_count=3,
        intents=[
            IntentSnapshot(id="i002", status="running", creator="reasoner"),
            IntentSnapshot(id="i003", status="running", creator="reasoner"),
            IntentSnapshot(id="i004", status="running", creator="reasoner"),
        ],
    )
    assert [item.kind for item in Reconciler(now=lambda: now).calculate([replan])] == [
        "reason"
    ]


def test_retry_policies_preserve_existing_behavior() -> None:
    explore = ExploreRetryPolicy()
    assert [explore.execute_timeout(240, retry) for retry in range(4)] == [240, 480, 960, 960]
    assert [explore.conclude_timeout(90, retry) for retry in range(4)] == [90, 180, 270, 270]
    assert explore.can_attempt(3) is True
    assert explore.can_attempt(4) is False
    assert explore.can_attempt(
        4,
        failure_class="transient",
        infrastructure_retry_count=3,
    ) is True
    assert explore.can_attempt(
        0,
        failure_class="transient",
        infrastructure_retry_count=4,
    ) is False

    bootstrap = BootstrapRetryPolicy()
    first = bootstrap.after_rejection(previous_count=0, signature="s1", now_epoch=100)
    third = bootstrap.after_rejection(previous_count=2, signature="s1", now_epoch=100)
    assert first.retry_count == 1 and first.next_retry_epoch == 130 and first.needs_hint is False
    assert third.retry_count == 3 and third.needs_hint is True


def test_reconciler_retries_transient_failures_without_spending_execution_budget() -> None:
    recoverable = task(
        phase="explore",
        reason_due=False,
        intents=[
            IntentSnapshot(
                id="i002",
                status="pending",
                creator="reasoner",
                retry_count=4,
                infrastructure_retry_count=3,
                last_failure_kind="v2protocolerror",
                failure_class="transient",
            )
        ],
    )
    assert [(item.kind, item.intent_id) for item in Reconciler().calculate([recoverable])] == [
        ("explore", "i002")
    ]

    exhausted = recoverable.model_copy(
        update={
            "intents": [
                recoverable.intents[0].model_copy(update={"infrastructure_retry_count": 4})
            ]
        }
    )
    assert [(item.kind, item.intent_id) for item in Reconciler().calculate([exhausted])] == [
        ("needs_replan", "i002")
    ]


def test_reconciler_sends_exhausted_execution_failures_to_reason() -> None:
    exhausted = task(
        phase="explore",
        reason_due=False,
        intents=[
            IntentSnapshot(
                id="i002",
                status="pending",
                creator="reasoner",
                retry_count=4,
                last_failure_kind="timeout",
                failure_class="execution",
            )
        ],
    )
    assert [(item.kind, item.intent_id) for item in Reconciler().calculate([exhausted])] == [
        ("needs_replan", "i002")
    ]


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
