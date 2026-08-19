from __future__ import annotations

import random
from collections.abc import Callable
from datetime import datetime, timezone

from axiom.dispatcher.schemas import (
    ACTIVE_WORK_STATUSES,
    ClaimAction,
    DispatchAction,
    DispatcherSnapshot,
    IntentSnapshot,
    ReasonRunSnapshot,
    ReviewRunSnapshot,
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
        active_review_by_task = {
            task.id: sum(
                1 for item in task.review_runs if item.status in ACTIVE_WORK_STATUSES
            )
            for task in snapshot.tasks
        }
        global_active = sum(active_by_task.values())
        active_task_ids = {task_id for task_id, count in active_by_task.items() if count > 0}
        ordered_tasks = sorted(
            (
                task
                for task in snapshot.tasks
                if task.status == "running"
                or any(item.status == "pending" for item in task.review_runs)
            ),
            key=lambda item: (
                not self._has_ready_main_work(item, now),
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
                    active_review=active_review_by_task[task.id],
                    active_explore=active_explore_by_task[task.id],
                    explore_limit=limits.per_task_explore_concurrency,
                    review_limit=limits.per_task_review_concurrency,
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
                elif work.kind == "review":
                    active_review_by_task[task.id] += 1
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
                            review_run_id=(
                                work.id if isinstance(work, ReviewRunSnapshot) else None
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
    def _has_ready_main_work(task: TaskSnapshot, now: datetime) -> bool:
        if task.status != "running":
            return False
        return any(
            item.status == "pending"
            and item.kind in {"bootstrap", "explore"}
            and (item.next_retry_at is None or item.next_retry_at <= now)
            for item in task.intents
        ) or any(
            item.status == "pending"
            and (item.next_retry_at is None or item.next_retry_at <= now)
            for item in task.reason_runs
        )

    @staticmethod
    def _active_count(task: TaskSnapshot) -> int:
        return sum(item.status in ACTIVE_WORK_STATUSES for item in task.intents) + sum(
            item.status in ACTIVE_WORK_STATUSES for item in task.reason_runs
        ) + sum(item.status in ACTIVE_WORK_STATUSES for item in task.review_runs)

    def _select_candidate(
        self,
        task: TaskSnapshot,
        *,
        now: datetime,
        workers: dict[str, WorkerSnapshot],
        reserved_workers: dict[str, int],
        active_bootstrap: int,
        active_reason: int,
        active_review: int,
        active_explore: int,
        explore_limit: int,
        review_limit: int,
    ) -> tuple[IntentSnapshot | ReasonRunSnapshot | ReviewRunSnapshot, WorkerSnapshot] | None:
        pending_reasons = sorted(
            (
                item
                for item in task.reason_runs
                if task.status == "running" and item.status == "pending"
            ),
            key=lambda item: (item.created_at, item.id),
        )
        candidates: list[IntentSnapshot | ReasonRunSnapshot | ReviewRunSnapshot] = [
            *(
                item
                for item in task.intents
                if task.status == "running"
                and item.status == "pending"
                and item.kind in {"bootstrap", "explore"}
            ),
            *(pending_reasons[:1]),
            *(item for item in task.review_runs if item.status == "pending"),
        ]
        candidates.sort(
            key=lambda item: (
                # Reviewer is independent post-processing.  Keep all main-chain
                # work ahead of it so a newly reported vulnerability cannot
                # consume the Task's last slot before its Explore work.
                {"bootstrap": 0, "reason": 1, "explore": 2, "review": 3}[item.kind],
                # Intent priority is owned by Reason and is only meaningful
                # within a work kind.  Higher values run first; old snapshots
                # without the field retain the neutral default.
                -self._work_priority(item),
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
            if work.kind == "review" and active_review >= review_limit:
                continue
            if work.kind == "explore" and active_explore >= explore_limit:
                continue
            worker = self._select_worker(work, workers, reserved_workers)
            if worker is not None:
                return work, worker
        return None

    @staticmethod
    def _work_priority(
        work: IntentSnapshot | ReasonRunSnapshot | ReviewRunSnapshot,
    ) -> int:
        """Return the scheduler priority for a pending work item.

        Only Intent rows carry an Intent priority.  ``getattr`` is deliberate:
        dispatchers can briefly talk to an older API during a rolling upgrade,
        in which case the neutral value preserves the existing FIFO behavior.
        """

        if isinstance(work, IntentSnapshot):
            return int(getattr(work, "priority", 50))
        return 50

    @staticmethod
    def _remove_pending(
        task: TaskSnapshot,
        work: IntentSnapshot | ReasonRunSnapshot | ReviewRunSnapshot,
    ) -> None:
        if isinstance(work, IntentSnapshot):
            collection = task.intents
        elif isinstance(work, ReasonRunSnapshot):
            collection = task.reason_runs
        else:
            collection = task.review_runs
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
        work: IntentSnapshot | ReasonRunSnapshot | ReviewRunSnapshot,
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
        if not candidates:
            return None
        best_priority = min(item.priority for item in candidates)
        priority_candidates = [
            item for item in candidates if item.priority == best_priority
        ]
        least_reserved = min(reserved[item.name] for item in priority_candidates)
        balanced_candidates = [
            item for item in priority_candidates if reserved[item.name] == least_reserved
        ]
        return random.choice(balanced_candidates)
