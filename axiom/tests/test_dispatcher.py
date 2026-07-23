from __future__ import annotations

from datetime import datetime, timedelta, timezone

from axiom.dispatcher.loop import Dispatcher
from axiom.dispatcher.reconciler import Reconciler
from axiom.dispatcher.schemas import (
    ClaimedIntent,
    DispatcherSnapshot,
    IntentSnapshot,
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
