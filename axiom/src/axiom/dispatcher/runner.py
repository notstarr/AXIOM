from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor

from axiom.dispatcher.bootstrap import BootstrapRunner
from axiom.dispatcher.heartbeat import LeaseLostError
from axiom.dispatcher.http import ControlPlaneError
from axiom.dispatcher.loop import Dispatcher
from axiom.dispatcher.explore import ExploreRunner
from axiom.dispatcher.reason import ReasonRunner
from axiom.dispatcher.schemas import ClaimedIntent, DispatchReport
from axiom.modules.execution.schemas import ActivityCommand, ReleaseIntentCommand
from axiom.modules.workers.schemas import Worker


logger = logging.getLogger(__name__)


class AxiomRunner:
    """Reconcile frequently while claimed work executes in bounded threads."""

    def __init__(
        self,
        control_plane,
        *,
        executor_factory: Callable[[Worker, ClaimedIntent], object] | None = None,
        max_parallel: int | None = None,
        heartbeat_interval_seconds: float | None = None,
        lease_timeout_seconds: int | None = None,
    ):
        if max_parallel is not None and max_parallel <= 0:
            raise ValueError("max_parallel must be positive")
        if heartbeat_interval_seconds is not None and heartbeat_interval_seconds <= 0:
            raise ValueError("heartbeat_interval_seconds must be positive")
        heartbeat = heartbeat_interval_seconds or 10.0
        minimum_lease_timeout = max(20, int(heartbeat * 3))
        if lease_timeout_seconds is not None and lease_timeout_seconds < minimum_lease_timeout:
            raise ValueError("lease_timeout_seconds must allow at least three heartbeats")
        self.control_plane = control_plane
        self.dispatcher = Dispatcher(control_plane)
        self.executor_factory = executor_factory or self._default_executor
        self._max_parallel_override = max_parallel
        self._heartbeat_override = heartbeat_interval_seconds
        self._lease_timeout_override = lease_timeout_seconds
        self.max_parallel = max_parallel or 8
        self.heartbeat_interval_seconds = heartbeat
        self.lease_timeout_seconds = lease_timeout_seconds or minimum_lease_timeout
        self.runner_interval_seconds = 1.0
        self._pool = ThreadPoolExecutor(
            max_workers=128,
            thread_name_prefix="axiom-runner",
        )
        self._futures: dict[Future, tuple[ClaimedIntent, threading.Event]] = {}
        self._lock = threading.Lock()
        self._closed = False

    def run_once(self) -> DispatchReport:
        if self._closed:
            raise RuntimeError("Axiom Runner is closed")
        self._refresh_runtime_settings()
        recover = getattr(self.control_plane, "recover_stale", None)
        if recover is not None:
            recover(stale_after_seconds=self.lease_timeout_seconds)
        self._reap_finished()
        snapshot = self.control_plane.snapshot()
        self._cancel_inactive_tasks(snapshot)
        with self._lock:
            available_slots = self.max_parallel - len(self._futures)
        if available_slots <= 0:
            return DispatchReport()
        report = self.dispatcher.tick(max_claims=available_slots, snapshot=snapshot)
        for work in report.claims:
            cancellation = threading.Event()
            future = self._pool.submit(self._execute_claim, work, cancellation)
            with self._lock:
                self._futures[future] = (work, cancellation)
        return report

    def run_forever(
        self,
        *,
        stop_event: threading.Event,
        interval_seconds: float | None = None,
    ) -> None:
        if interval_seconds is not None and interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        while not stop_event.is_set():
            try:
                self.run_once()
            except ControlPlaneError as exc:
                # The API and Runner are separate processes.  During an API
                # restart the control plane can be briefly unavailable; that
                # is a normal reconciliation condition, not a reason to kill
                # the long-lived Runner process.
                logger.warning(
                    "control plane unavailable; reconciliation will retry: %s",
                    exc,
                )
            stop_event.wait(interval_seconds or self.runner_interval_seconds)

    def wait_for_idle(self, timeout_seconds: float = 30.0) -> None:
        deadline = time.monotonic() + timeout_seconds
        while True:
            self._reap_finished()
            with self._lock:
                pending = bool(self._futures)
            if not pending:
                return
            if time.monotonic() >= deadline:
                raise TimeoutError("Axiom Runner did not become idle")
            time.sleep(0.05)

    def close(self, *, wait: bool = True) -> None:
        if self._closed:
            return
        self._closed = True
        with self._lock:
            for _work, cancellation in self._futures.values():
                cancellation.set()
        self._pool.shutdown(wait=wait, cancel_futures=not wait)

    def _execute_claim(self, work: ClaimedIntent, cancellation: threading.Event) -> None:
        try:
            worker = self.control_plane.worker(work.worker_name)
            executor = self.executor_factory(worker, work)
        except BaseException:
            logger.exception("failed to initialize Intent runner: %s/%s", work.task_id, work.intent_id)
            self._release_unstarted(work)
            return

        try:
            if work.kind == "bootstrap":
                BootstrapRunner(
                    self.control_plane,
                    heartbeat_interval_seconds=self.heartbeat_interval_seconds,
                    cancellation=cancellation,
                ).run(work, executor)
            elif work.kind == "reason":
                ReasonRunner(
                    self.control_plane,
                    heartbeat_interval_seconds=self.heartbeat_interval_seconds,
                    cancellation=cancellation,
                ).run(work, executor)
            elif work.kind == "explore":
                ExploreRunner(
                    self.control_plane,
                    heartbeat_interval_seconds=self.heartbeat_interval_seconds,
                    cancellation=cancellation,
                ).run(work, executor)
            else:
                raise RuntimeError(f"unsupported Intent kind: {work.kind}")
        except LeaseLostError:
            logger.info(
                "Intent execution ended after its lease was closed: %s/%s",
                work.task_id,
                work.intent_id,
            )
        except BaseException:
            # BootstrapRunner owns lifecycle release.  This layer only records
            # the failure; issuing a second release would race the next claim.
            logger.exception("Intent execution failed: %s/%s", work.task_id, work.intent_id)

    def _release_unstarted(self, work: ClaimedIntent) -> None:
        try:
            self.control_plane.release(
                work,
                ReleaseIntentCommand(
                    lease_id=work.lease_id,
                    failure_kind="runner_dispatch_failed",
                    failure_class="transient",
                ),
            )
        except BaseException:
            logger.exception("failed to release runner claim: %s/%s", work.task_id, work.intent_id)

    def _default_executor(self, worker: Worker, work: ClaimedIntent) -> object:
        from axiom.dispatcher.pi import PiBootstrapExecutor, PiExploreExecutor, PiReasonExecutor

        def activity_reporter(command: ActivityCommand) -> None:
            self.control_plane.activity(work, command)

        if work.kind == "bootstrap":
            return PiBootstrapExecutor(worker, activity_reporter=activity_reporter)
        if work.kind == "reason":
            return PiReasonExecutor(worker, activity_reporter=activity_reporter)
        if work.kind == "explore":
            return PiExploreExecutor(worker, activity_reporter=activity_reporter)
        raise RuntimeError(f"unsupported Intent kind: {work.kind}")

    def _reap_finished(self) -> None:
        with self._lock:
            finished = [future for future in self._futures if future.done()]
            for future in finished:
                self._futures.pop(future, None)

    def _cancel_inactive_tasks(self, snapshot) -> None:
        active_task_ids = {task.id for task in snapshot.tasks if task.status == "running"}
        with self._lock:
            for work, cancellation in self._futures.values():
                if work.task_id not in active_task_ids:
                    cancellation.set()

    def _refresh_runtime_settings(self) -> None:
        provider = getattr(self.control_plane, "runtime_settings", None)
        if provider is None:
            return
        try:
            config = provider().config
        except ControlPlaneError:
            # Let run_forever handle a temporary control-plane outage once,
            # without producing a second traceback from this optional refresh.
            raise
        except Exception:
            logger.warning("failed to refresh runtime settings; keeping last known values", exc_info=True)
            return
        heartbeat = self._heartbeat_override or float(config.heartbeat_interval_seconds)
        lease_timeout = self._lease_timeout_override or int(config.lease_timeout_seconds)
        minimum = max(20, int(heartbeat * 3))
        self.max_parallel = self._max_parallel_override or int(config.max_parallel)
        self.heartbeat_interval_seconds = heartbeat
        self.lease_timeout_seconds = max(lease_timeout, minimum)
        self.runner_interval_seconds = float(config.runner_interval_seconds)
