from __future__ import annotations

from datetime import datetime, timedelta, timezone

from axiom.dispatcher.loop import Dispatcher
from axiom.dispatcher.reconciler import Reconciler
from axiom.dispatcher.schemas import (
    ClaimedIntent,
    DispatchLimits,
    DispatcherSnapshot,
    IntentSnapshot,
    ReasonRunSnapshot,
    ReviewRunSnapshot,
    TaskSnapshot,
    WorkerSnapshot,
)


NOW = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)


class FakeControlPlane:
    def __init__(self, snapshot: DispatcherSnapshot):
        self.current = snapshot
        self.claims: list[tuple[str, str, str]] = []

    def snapshot(self) -> DispatcherSnapshot:
        return self.current

    def claim(self, *, task_id: str, intent_id: str, worker_name: str) -> ClaimedIntent | None:
        for task in self.current.tasks:
            if task.id != task_id:
                continue
            for intent in task.intents:
                if intent.id == intent_id and intent.status == "pending":
                    intent.status = "claimed"
                    self.claims.append((task_id, intent_id, worker_name))
                    return ClaimedIntent(
                        task_id=task_id,
                        intent_id=intent_id,
                        worker_name=worker_name,
                        kind=intent.kind,
                        claimed_at="2026-07-23T12:00:00Z",
                        lease_id=f"lease-{intent_id}",
                    )
        return None


def intent(task_id: str, number: int, *, kind="explore", pool=None, retry_at=None):
    return IntentSnapshot(
        task_id=task_id,
        id=f"i{number:03d}",
        kind=kind,
        status="pending",
        worker_pool=pool or [],
        next_retry_at=retry_at,
        created_at=NOW + timedelta(seconds=number),
    )


def worker(name: str, *, capabilities=("explore",), running=0, max_running=1, priority=0):
    return WorkerSnapshot(
        name=name,
        capabilities=frozenset(capabilities),
        running=running,
        max_running=max_running,
        priority=priority,
    )


def reason_run(task_id: str, number: int, *, status="pending", pool=None):
    return ReasonRunSnapshot(
        task_id=task_id,
        id=f"rr{number:06d}",
        status=status,
        worker_pool=pool or [],
        created_at=NOW + timedelta(seconds=number),
        claimed_at=NOW if status in {"claimed", "running", "concluding"} else None,
    )


def review_run(task_id: str, number: int, *, status="pending", pool=None):
    return ReviewRunSnapshot(
        task_id=task_id,
        id=f"vr{number:06d}",
        vulnerability_id=f"v{number:03d}",
        status=status,
        worker_pool=pool or [],
        created_at=NOW + timedelta(seconds=number),
        claimed_at=NOW if status in {"claimed", "running", "concluding"} else None,
    )


def test_reconciler_assigns_pending_intents_to_idle_workers_without_reason_assignment():
    snapshot = DispatcherSnapshot(
        tasks=[TaskSnapshot(id="t_001", status="running", intents=[intent("t_001", 2), intent("t_001", 3)])],
        workers=[worker("worker-a"), worker("worker-b", priority=1)],
    )

    actions = Reconciler(now=lambda: NOW).calculate(snapshot)

    assert [(item.claim.intent_id, item.claim.worker_name) for item in actions] == [
        ("i002", "worker-a"),
        ("i003", "worker-b"),
    ]


def test_reconciler_randomizes_equal_priority_workers(monkeypatch):
    monkeypatch.setattr(
        "axiom.dispatcher.reconciler.random.choice",
        lambda candidates: next(item for item in candidates if item.name == "worker-b"),
    )
    snapshot = DispatcherSnapshot(
        tasks=[TaskSnapshot(id="t_001", status="running", intents=[intent("t_001", 2)])],
        workers=[worker("worker-a"), worker("worker-b")],
    )

    actions = Reconciler(now=lambda: NOW).calculate(snapshot)

    assert actions[0].claim.worker_name == "worker-b"


def test_reconciler_respects_capability_pool_capacity_and_retry_time():
    snapshot = DispatcherSnapshot(
        tasks=[
            TaskSnapshot(
                id="t_001",
                status="running",
                intents=[
                    intent("t_001", 2, kind="bootstrap"),
                    intent("t_001", 3, pool=["worker-b"]),
                    intent("t_001", 4, retry_at=NOW + timedelta(minutes=1)),
                ],
            )
        ],
        workers=[
            worker("worker-a", capabilities=("explore",), max_running=1),
            worker("worker-b", capabilities=("bootstrap", "explore"), max_running=1),
        ],
    )

    actions = Reconciler(now=lambda: NOW).calculate(snapshot)

    assert [(item.claim.intent_id, item.claim.worker_name) for item in actions] == [
        ("i002", "worker-b"),
    ]


def test_dispatcher_treats_lost_race_as_a_conflict_and_does_not_duplicate_state():
    control_plane = FakeControlPlane(
        DispatcherSnapshot(
            tasks=[TaskSnapshot(id="t_001", status="running", intents=[intent("t_001", 2)])],
            workers=[worker("worker-a")],
        )
    )
    dispatcher = Dispatcher(control_plane, reconciler=Reconciler(now=lambda: NOW))

    first = dispatcher.tick()
    second = dispatcher.tick()

    assert first.considered == 1
    assert first.planned == 1
    assert first.claimed == 1
    assert first.conflicts == 0
    assert [item.intent_id for item in first.claims] == ["i002"]
    assert second.model_dump() == {
        "considered": 0,
        "planned": 0,
        "claimed": 0,
        "conflicts": 0,
        "claims": [],
    }
    assert control_plane.claims == [("t_001", "i002", "worker-a")]


def test_dispatcher_limits_claims_to_runner_capacity():
    control_plane = FakeControlPlane(
        DispatcherSnapshot(
            tasks=[
                TaskSnapshot(
                    id="t_001",
                    status="running",
                    intents=[intent("t_001", 2), intent("t_001", 3)],
                )
            ],
            workers=[worker("worker-a", max_running=2)],
        )
    )

    report = Dispatcher(control_plane, reconciler=Reconciler(now=lambda: NOW)).tick(
        max_claims=1
    )

    assert report.claimed == 1
    assert [item.intent_id for item in report.claims] == ["i002"]
    assert control_plane.current.tasks[0].intents[1].status == "pending"


def test_reconciler_uses_bootstrap_reason_explore_priority_without_reason_intents():
    snapshot = DispatcherSnapshot(
        tasks=[
            TaskSnapshot(
                id="t_001",
                status="running",
                intents=[
                    intent("t_001", 1, kind="bootstrap"),
                    intent("t_001", 2, kind="explore"),
                ],
                reason_runs=[reason_run("t_001", 1)],
            )
        ],
        workers=[
            worker("bootstrap", capabilities=("bootstrap",)),
            worker("reason", capabilities=("reason",)),
            worker("explore", capabilities=("explore",)),
        ],
        limits=DispatchLimits(
            global_max_active_runs=3,
            global_max_active_tasks=1,
            per_task_max_active_runs=3,
            per_task_explore_concurrency=1,
        ),
    )

    actions = Reconciler(now=lambda: NOW).calculate(snapshot)

    assert [item.claim.kind for item in actions] == ["bootstrap", "reason", "explore"]
    assert actions[1].claim.reason_run_id == "rr000001"
    assert actions[1].claim.intent_id is None


def test_reconciler_round_robins_tasks_before_giving_one_task_a_second_slot():
    snapshot = DispatcherSnapshot(
        tasks=[
            TaskSnapshot(
                id="t_001",
                status="running",
                intents=[intent("t_001", 2), intent("t_001", 3)],
            ),
            TaskSnapshot(
                id="t_002",
                status="running",
                intents=[intent("t_002", 2), intent("t_002", 3)],
            ),
        ],
        workers=[worker("worker-a", max_running=2)],
        limits=DispatchLimits(
            global_max_active_runs=2,
            global_max_active_tasks=2,
            per_task_max_active_runs=2,
            per_task_explore_concurrency=2,
        ),
    )

    actions = Reconciler(now=lambda: NOW).calculate(snapshot)

    assert [(item.claim.task_id, item.claim.intent_id) for item in actions] == [
        ("t_001", "i002"),
        ("t_002", "i002"),
    ]


def test_reconciler_prefers_the_task_that_has_waited_longer_across_ticks():
    snapshot = DispatcherSnapshot(
        tasks=[
            TaskSnapshot(
                id="t_001",
                status="running",
                intents=[intent("t_001", 2)],
                last_claimed_at=NOW,
            ),
            TaskSnapshot(
                id="t_002",
                status="running",
                intents=[intent("t_002", 2)],
            ),
        ],
        workers=[worker("worker-a")],
    )

    actions = Reconciler(now=lambda: NOW).calculate(snapshot)

    assert actions[0].claim.task_id == "t_002"


def test_active_reason_run_consumes_global_task_and_reason_capacity():
    snapshot = DispatcherSnapshot(
        tasks=[
            TaskSnapshot(
                id="t_001",
                status="running",
                intents=[intent("t_001", 2)],
                reason_runs=[
                    reason_run("t_001", 1, status="running"),
                    reason_run("t_001", 2),
                ],
            ),
            TaskSnapshot(
                id="t_002",
                status="running",
                intents=[intent("t_002", 2)],
            ),
        ],
        workers=[
            worker("reason-a", capabilities=("reason",), running=1, max_running=2),
            worker("explore-a", capabilities=("explore",)),
        ],
        limits=DispatchLimits(
            global_max_active_runs=2,
            global_max_active_tasks=1,
            per_task_max_active_runs=2,
            per_task_explore_concurrency=1,
        ),
    )

    actions = Reconciler(now=lambda: NOW).calculate(snapshot)

    assert len(actions) == 1
    assert actions[0].claim.task_id == "t_001"
    assert actions[0].claim.kind == "explore"


def test_reconciler_does_not_exceed_per_task_explore_concurrency():
    active = intent("t_001", 2)
    active.status = "running"
    active.claimed_at = NOW
    snapshot = DispatcherSnapshot(
        tasks=[
            TaskSnapshot(
                id="t_001",
                status="running",
                intents=[active, intent("t_001", 3)],
            )
        ],
        workers=[worker("worker-a", running=1, max_running=2)],
        limits=DispatchLimits(
            global_max_active_runs=2,
            global_max_active_tasks=1,
            per_task_max_active_runs=2,
            per_task_explore_concurrency=1,
        ),
    )

    assert Reconciler(now=lambda: NOW).calculate(snapshot) == []


def test_pending_review_is_schedulable_for_a_completed_task() -> None:
    snapshot = DispatcherSnapshot(
        tasks=[
            TaskSnapshot(
                id="t_001",
                status="completed",
                review_runs=[review_run("t_001", 1)],
            )
        ],
        workers=[worker("reviewer-a", capabilities=("review",))],
    )

    actions = Reconciler(now=lambda: NOW).calculate(snapshot)

    assert len(actions) == 1
    assert actions[0].claim.kind == "review"
    assert actions[0].claim.review_run_id == "vr000001"
    assert actions[0].claim.intent_id is None


def test_reconciler_schedules_reviews_up_to_the_configured_per_task_limit() -> None:
    snapshot = DispatcherSnapshot(
        tasks=[
            TaskSnapshot(
                id="t_001",
                status="completed",
                review_runs=[review_run("t_001", number) for number in range(1, 6)],
            )
        ],
        workers=[worker("reviewer-a", capabilities=("review",), max_running=5)],
        limits=DispatchLimits(
            global_max_active_runs=5,
            global_max_active_tasks=1,
            per_task_max_active_runs=5,
            per_task_explore_concurrency=4,
            per_task_review_concurrency=4,
        ),
    )

    actions = Reconciler(now=lambda: NOW).calculate(snapshot)

    assert len(actions) == 4
    assert [item.claim.review_run_id for item in actions] == [
        "vr000001",
        "vr000002",
        "vr000003",
        "vr000004",
    ]


def test_reconciler_stops_new_reviews_at_the_configured_per_task_limit() -> None:
    snapshot = DispatcherSnapshot(
        tasks=[
            TaskSnapshot(
                id="t_001",
                status="completed",
                review_runs=[
                    review_run("t_001", 1, status="running"),
                    review_run("t_001", 2),
                ],
            )
        ],
        workers=[
            worker(
                "reviewer-a",
                capabilities=("review",),
                running=1,
                max_running=4,
            )
        ],
        limits=DispatchLimits(
            global_max_active_runs=4,
            global_max_active_tasks=1,
            per_task_max_active_runs=4,
            per_task_explore_concurrency=4,
            per_task_review_concurrency=1,
        ),
    )

    assert Reconciler(now=lambda: NOW).calculate(snapshot) == []


def test_reconciler_keeps_main_chain_explore_ahead_of_reviewer() -> None:
    snapshot = DispatcherSnapshot(
        tasks=[
            TaskSnapshot(
                id="t_001",
                status="running",
                intents=[intent("t_001", 3)],
                review_runs=[review_run("t_001", 1)],
            )
        ],
        workers=[
            worker("explore-a", capabilities=("explore",)),
            worker("reviewer-a", capabilities=("review",)),
        ],
        limits=DispatchLimits(
            global_max_active_runs=1,
            global_max_active_tasks=1,
            per_task_max_active_runs=1,
            per_task_explore_concurrency=1,
        ),
    )

    actions = Reconciler(now=lambda: NOW).calculate(snapshot)

    assert len(actions) == 1
    assert actions[0].claim.kind == "explore"
    assert actions[0].claim.intent_id == "i003"


def test_reconciler_keeps_running_task_main_chain_ahead_of_other_task_review() -> None:
    snapshot = DispatcherSnapshot(
        tasks=[
            TaskSnapshot(
                id="t_review",
                status="completed",
                review_runs=[review_run("t_review", 1)],
                created_at=NOW - timedelta(days=1),
            ),
            TaskSnapshot(
                id="t_main",
                status="running",
                intents=[intent("t_main", 3)],
                created_at=NOW,
            ),
        ],
        workers=[
            worker("explore-a", capabilities=("explore",)),
            worker("reviewer-a", capabilities=("review",)),
        ],
        limits=DispatchLimits(
            global_max_active_runs=1,
            global_max_active_tasks=1,
            per_task_max_active_runs=1,
            per_task_explore_concurrency=1,
        ),
    )

    actions = Reconciler(now=lambda: NOW).calculate(snapshot)

    assert len(actions) == 1
    assert actions[0].claim.task_id == "t_main"
    assert actions[0].claim.kind == "explore"


def test_per_task_review_limit_does_not_block_unrelated_explore_work() -> None:
    snapshot = DispatcherSnapshot(
        tasks=[
            TaskSnapshot(
                id="t_001",
                status="running",
                intents=[intent("t_001", 3)],
                review_runs=[
                    review_run("t_001", 1, status="running"),
                    review_run("t_001", 2),
                ],
            )
        ],
        workers=[
            worker("reviewer-a", capabilities=("review",), running=1, max_running=2),
            worker("explore-a", capabilities=("explore",)),
        ],
        limits=DispatchLimits(
            global_max_active_runs=2,
            global_max_active_tasks=1,
            per_task_max_active_runs=2,
            per_task_explore_concurrency=1,
        ),
    )

    actions = Reconciler(now=lambda: NOW).calculate(snapshot)

    assert len(actions) == 1
    assert actions[0].claim.kind == "explore"
    assert actions[0].claim.intent_id == "i003"


def test_review_uses_only_review_capable_backends_and_shared_capacity() -> None:
    task = TaskSnapshot(
        id="t_001",
        status="stopped",
        review_runs=[review_run("t_001", 1)],
    )
    no_capacity = DispatcherSnapshot(
        tasks=[task],
        workers=[
            worker(
                "shared-backend",
                capabilities=("reason", "review"),
                running=1,
                max_running=1,
            ),
            worker("reason-only", capabilities=("reason",)),
        ],
    )
    assert Reconciler(now=lambda: NOW).calculate(no_capacity) == []

    available = no_capacity.model_copy(deep=True)
    available.workers[0].running = 0
    actions = Reconciler(now=lambda: NOW).calculate(available)
    assert len(actions) == 1
    assert actions[0].claim.worker_name == "shared-backend"
    assert actions[0].claim.kind == "review"
