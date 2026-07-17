from __future__ import annotations

import logging
from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from threading import Event

from axiom.dispatcher.v2.client import V2Client
from axiom.dispatcher.v2.reconciler import Reconciler
from axiom.dispatcher.v2.runners import PhaseRunners
from axiom.dispatcher.v2.schemas import DispatchAction
from axiom.dispatcher.v2.worker_allocator import WorkerAllocator


LOG = logging.getLogger(__name__)


@dataclass(slots=True)
class LiveRun:
    action: DispatchAction
    worker: str


class V2DispatcherLoop:
    def __init__(
        self,
        *,
        server: str,
        service_token: str,
        client: V2Client | None = None,
        runners: PhaseRunners | None = None,
    ):
        self.client = client or V2Client(server, service_token=service_token)
        self.runners = runners or PhaseRunners(self.client)
        self.reconciler = Reconciler()
        self.allocator = WorkerAllocator()
        self.executor: ThreadPoolExecutor | None = None
        self.futures: dict[Future, LiveRun] = {}
        self.stop_event = Event()

    def run(self, *, once: bool = False) -> None:
        try:
            while not self.stop_event.is_set():
                runtime = self.client.runtime_settings().config
                self._ensure_executor(runtime.max_workers)
                self._reap()
                recovered = self.client.recover_stale_leases(runtime.lease_timeout_seconds)
                if recovered.get("intents_released") or recovered.get("reasons_released"):
                    LOG.warning("recovered stale leases %s", recovered)
                snapshot = self.client.snapshot()
                actions = self.reconciler.calculate(snapshot.tasks)
                self._dispatch(actions, snapshot.tasks, snapshot.workers, runtime)
                if once:
                    return
                self.stop_event.wait(runtime.interval_seconds)
        finally:
            self.close()

    def stop(self) -> None:
        self.stop_event.set()

    def close(self) -> None:
        if self.executor is not None:
            self.executor.shutdown(wait=True)
            self.executor = None
        self.client.close()

    def _ensure_executor(self, max_workers: int) -> None:
        if self.executor is None:
            self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="axiom-v2")

    def _reap(self) -> None:
        for future, live in list(self.futures.items()):
            if not future.done():
                continue
            self.futures.pop(future)
            try:
                future.result()
            except Exception:
                LOG.exception(
                    "phase failed kind=%s task=%s intent=%s worker=%s",
                    live.action.kind,
                    live.action.task_id,
                    live.action.intent_id,
                    live.worker,
                )

    def _dispatch(self, actions, tasks, workers, runtime) -> None:
        if self.executor is None:
            return
        live_keys = {self._key(live.action) for live in self.futures.values()}
        task_counts = Counter(live.action.task_id for live in self.futures.values())
        task_ids = set(task_counts)
        phase_counts = Counter(live.action.kind for live in self.futures.values())
        worker_counts = Counter(live.worker for live in self.futures.values())
        limits = {
            "bootstrap": runtime.bootstrap_limit,
            "reason": runtime.reason_limit,
            "explore": runtime.explore_limit,
        }
        task_pools = {task.id: task.worker_pool for task in tasks}
        for action in actions:
            key = self._key(action)
            if key in live_keys:
                continue
            if action.kind == "needs_replan":
                self.client.needs_replan(action.task_id, action.intent_id or "")
                live_keys.add(key)
                continue
            if len(self.futures) >= runtime.max_workers:
                break
            if task_counts[action.task_id] >= runtime.max_task_workers:
                continue
            if action.task_id not in task_ids and len(task_ids) >= runtime.max_running_tasks:
                continue
            if phase_counts[action.kind] >= limits[action.kind]:
                continue
            candidates = [
                item.model_copy(update={"running": item.running + worker_counts[item.name]})
                for item in workers
            ]
            worker = self.allocator.choose(
                candidates,
                capability=action.kind,
                task_pool=task_pools.get(action.task_id, []),
            )
            if worker is None:
                continue
            future = self.executor.submit(self.runners.run, action, worker, runtime)
            self.futures[future] = LiveRun(action=action, worker=worker.name)
            live_keys.add(key)
            task_counts[action.task_id] += 1
            task_ids.add(action.task_id)
            phase_counts[action.kind] += 1
            worker_counts[worker.name] += 1

    @staticmethod
    def _key(action: DispatchAction) -> tuple[str, str, str | None]:
        return action.kind, action.task_id, action.intent_id
