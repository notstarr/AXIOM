from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from axiom.dispatcher.schemas import (
    ClaimAction,
    DispatchAction,
    DispatcherSnapshot,
    IntentSnapshot,
    WorkerSnapshot,
)


class Reconciler:
    """Calculate deterministic, capacity-aware worker claims.

    Reconciliation is intentionally a pure function of a snapshot.  It does
    not mutate snapshots and it does not assume that an action will succeed;
    the control plane performs the final compare-and-swap claim.  A later tick
    will safely retry a stale or contested action.
    """

    def __init__(self, *, now: Callable[[], datetime] | None = None):
        self._now = now or (lambda: datetime.now(timezone.utc))

    def calculate(self, snapshot: DispatcherSnapshot) -> list[DispatchAction]:
        now = self._now()
        available = {
            worker.name: worker
            for worker in sorted(snapshot.workers, key=lambda item: (item.priority, item.name))
            if self._available(worker, now)
        }
        reserved: dict[str, int] = {name: worker.running for name, worker in available.items()}
        actions: list[DispatchAction] = []

        for task in sorted(snapshot.tasks, key=lambda item: item.id):
            if task.status != "running":
                continue
            for intent in sorted(
                (item for item in task.intents if item.status == "pending"),
                key=lambda item: (item.created_at, item.id),
            ):
                if not self._eligible(intent, now):
                    continue
                worker = self._select_worker(intent, available, reserved)
                if worker is None:
                    continue
                reserved[worker.name] += 1
                actions.append(
                    DispatchAction(
                        kind="claim",
                        claim=ClaimAction(
                            task_id=intent.task_id,
                            intent_id=intent.id,
                            worker_name=worker.name,
                        ),
                    )
                )
        return actions

    @staticmethod
    def _available(worker: WorkerSnapshot, now: datetime) -> bool:
        return (
            not worker.disabled
            and worker.running < worker.max_running
            and (worker.cooldown_until is None or worker.cooldown_until <= now)
        )

    @staticmethod
    def _eligible(intent: IntentSnapshot, now: datetime) -> bool:
        return intent.next_retry_at is None or intent.next_retry_at <= now

    @staticmethod
    def _select_worker(
        intent: IntentSnapshot,
        workers: dict[str, WorkerSnapshot],
        reserved: dict[str, int],
    ) -> WorkerSnapshot | None:
        candidates = [
            worker
            for worker in workers.values()
            if intent.kind in worker.capabilities
            and (not intent.worker_pool or worker.name in intent.worker_pool)
            and reserved[worker.name] < worker.max_running
        ]
        return min(candidates, key=lambda item: (item.priority, reserved[item.name], item.name), default=None)
