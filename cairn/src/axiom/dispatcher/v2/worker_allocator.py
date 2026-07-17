from __future__ import annotations

from datetime import datetime, timezone

from axiom.dispatcher.v2.schemas import WorkerSnapshot


class WorkerAllocator:
    def choose(
        self,
        workers: list[WorkerSnapshot],
        *,
        capability: str,
        task_pool: list[str],
        now: datetime | None = None,
    ) -> WorkerSnapshot | None:
        current = now or datetime.now(timezone.utc)
        pool = set(task_pool)
        eligible = [
            worker
            for worker in workers
            if not worker.disabled
            and capability in worker.capabilities
            and (not pool or worker.name in pool)
            and worker.running < worker.max_running
            and not _cooling_down(worker, current)
        ]
        if not eligible:
            return None
        return min(eligible, key=lambda item: (item.priority, item.running, item.name))


def _cooling_down(worker: WorkerSnapshot, now: datetime) -> bool:
    if not worker.cooldown_until:
        return False
    value = datetime.fromisoformat(worker.cooldown_until.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value > now
