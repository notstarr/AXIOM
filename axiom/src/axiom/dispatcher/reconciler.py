from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from axiom.dispatcher.schemas import (
    ACTIVE_WORK_STATUSES,
    ClaimAction,
    DispatchAction,
    DispatcherSnapshot,
    IntentSnapshot,
    ReasonRunSnapshot,
    TaskSnapshot,
    WorkerSnapshot,
)


class Reconciler:
    """Pure, capacity-aware and task-fair claim planning."""

    def __init__(self, *, now: Callable[[], datetime] | None = None):
        self._now = now or (lambda: datetime.now(timezone.utc))

    def calculate(self, snapshot: DispatcherSnapshot) -> list[DispatchAction]:
        # Candidate removal below is local scheduling state only.  Keep the
        # caller-owned snapshot immutable so metrics and retries observe the
        # same durable view that was reconciled.
        snapshot = snapshot.model_copy(deep=True)
        now = self._now()
        limits = snapshot.limits
        available = {
            worker.name: worker
            for worker in sorted(snapshot.workers, key=lambda item: (item.priority, item.name))
            if self._available(worker, now)
        }
        reserved_workers = {name: worker.running for name, worker in available.items()}
        active_by_task = {
            task.id: self._active_count(task)
            for task in snapshot.tasks
        }
        active_explore_by_task = {
            task.id: sum(
                1
                for item in task.intents
                if item.kind == "explore" and item.status in ACTIVE_WORK_STATUSES
            )
            for task in snapshot.tasks
        }
        active_bootstrap_by_task = {
            task.id: sum(
                1
                for item in task.intents
                if item.kind == "bootstrap" and item.status in ACTIVE_WORK_STATUSES
            )
            for task in snapshot.tasks
        }
        active_reason_by_task = {
            task.id: sum(
                1 for item in task.reason_runs if item.status in ACTIVE_WORK_STATUSES
            )
            for task in snapshot.tasks
        }
        global_active = sum(active_by_task.values())
        active_task_ids = {task_id for task_id, count in active_by_task.items() if count > 0}
        ordered_tasks = sorted(
            (task for task in snapshot.tasks if task.status == "running"),
            key=lambda item: (
                item.last_claimed_at is not None,
                item.last_claimed_at or item.created_at,
                item.created_at,
                item.id,
            ),
        )
        actions: list[DispatchAction] = []

        while global_active < limits.global_max_active_runs:
            progress = False
            for task in ordered_tasks:
                if global_active >= limits.global_max_active_runs:
                    break
                if active_by_task[task.id] >= limits.per_task_max_active_runs:
                    continue
                if (
                    task.id not in active_task_ids
                    and len(active_task_ids) >= limits.global_max_active_tasks
                ):
                    continue
                candidate = self._select_candidate(
                    task,
                    now=now,
                    workers=available,
                    reserved_workers=reserved_workers,
                    active_bootstrap=active_bootstrap_by_task[task.id],
                    active_reason=active_reason_by_task[task.id],
                    active_explore=active_explore_by_task[task.id],
                    explore_limit=limits.per_task_explore_concurrency,
                )
                if candidate is None:
                    continue
                work, worker = candidate
                reserved_workers[worker.name] += 1
                active_by_task[task.id] += 1
                global_active += 1
                active_task_ids.add(task.id)
                if work.kind == "bootstrap":
                    active_bootstrap_by_task[task.id] += 1
                elif work.kind == "reason":
                    active_reason_by_task[task.id] += 1
                else:
                    active_explore_by_task[task.id] += 1
                actions.append(
                    DispatchAction(
                        kind="claim",
                        claim=ClaimAction(
                            task_id=task.id,
                            worker_name=worker.name,
                            kind=work.kind,
                            intent_id=work.id if isinstance(work, IntentSnapshot) else None,
                            reason_run_id=(
                                work.id if isinstance(work, ReasonRunSnapshot) else None
                            ),
                        ),
                    )
                )
                self._remove_pending(task, work)
                progress = True
            if not progress:
                break
        return actions

    @staticmethod
    def _active_count(task: TaskSnapshot) -> int:
        return sum(item.status in ACTIVE_WORK_STATUSES for item in task.intents) + sum(
            item.status in ACTIVE_WORK_STATUSES for item in task.reason_runs
        )

    def _select_candidate(
        self,
        task: TaskSnapshot,
        *,
        now: datetime,
        workers: dict[str, WorkerSnapshot],
        reserved_workers: dict[str, int],
        active_bootstrap: int,
        active_reason: int,
        active_explore: int,
        explore_limit: int,
    ) -> tuple[IntentSnapshot | ReasonRunSnapshot, WorkerSnapshot] | None:
        candidates: list[IntentSnapshot | ReasonRunSnapshot] = [
            *(
                item
                for item in task.intents
                if item.status == "pending" and item.kind in {"bootstrap", "explore"}
            ),
            *(item for item in task.reason_runs if item.status == "pending"),
        ]
        candidates.sort(
            key=lambda item: (
                {"bootstrap": 0, "reason": 1, "explore": 2}[item.kind],
                item.created_at,
                item.id,
            )
        )
        for work in candidates:
            if work.next_retry_at is not None and work.next_retry_at > now:
                continue
            if work.kind == "bootstrap" and active_bootstrap >= 1:
                continue
            if work.kind == "reason" and active_reason >= 1:
                continue
            if work.kind == "explore" and active_explore >= explore_limit:
                continue
            worker = self._select_worker(work, workers, reserved_workers)
            if worker is not None:
                return work, worker
        return None

    @staticmethod
    def _remove_pending(
        task: TaskSnapshot,
        work: IntentSnapshot | ReasonRunSnapshot,
    ) -> None:
        collection = task.intents if isinstance(work, IntentSnapshot) else task.reason_runs
        collection.remove(work)

    @staticmethod
    def _available(worker: WorkerSnapshot, now: datetime) -> bool:
        return (
            not worker.disabled
            and worker.running < worker.max_running
            and (worker.cooldown_until is None or worker.cooldown_until <= now)
        )

    @staticmethod
    def _select_worker(
        work: IntentSnapshot | ReasonRunSnapshot,
        workers: dict[str, WorkerSnapshot],
        reserved: dict[str, int],
    ) -> WorkerSnapshot | None:
        candidates = [
            worker
            for worker in workers.values()
            if work.kind in worker.capabilities
            and (not work.worker_pool or worker.name in work.worker_pool)
            and reserved[worker.name] < worker.max_running
        ]
        return min(
            candidates,
            key=lambda item: (item.priority, reserved[item.name], item.name),
            default=None,
        )
