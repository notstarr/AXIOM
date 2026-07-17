from __future__ import annotations

import logging
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

from axiom.dispatcher.config import DispatchConfig, WorkerConfig
from axiom.dispatcher.models import ReasonCheckpoint, RunningTask
from axiom.dispatcher.protocol.client import CairnClient
from axiom.dispatcher.runtime.cancellation import TaskCancellation
from axiom.dispatcher.runtime.heartbeat import HeartbeatLease
from axiom.dispatcher.runtime.startup_healthcheck import format_failure_summary, run_startup_healthchecks
from axiom.dispatcher.scheduler.worker_select import choose_worker
from axiom.dispatcher import tasks  # imported for dispatch routing lookup
from axiom.dispatcher.tasks.bootstrap import run_bootstrap_task
from axiom.dispatcher.tasks.explore import MAX_EXPLORE_RETRIES, run_explore_task
from axiom.dispatcher.tasks.maintainer import run_maintainer_task
from axiom.dispatcher.tasks.plan import run_plan_task
from axiom.dispatcher.tasks.reason import run_reason_task
from axiom.server.dispatch_config_store import (
    DispatchConfigSnapshot,
    DispatchConfigVersion,
    load_dispatch_config_snapshot,
    peek_dispatch_config_version,
    write_dispatcher_runtime_state,
)
from axiom.server.models import Intent, ProjectDetail, ProjectSummary

LOG = logging.getLogger(__name__)
UNHEALTHY_RETRY_AFTER_SECONDS = 5
REJECTED_RETRY_AFTER_SECONDS = 5
BOOTSTRAP_REJECT_STALL_THRESHOLD = 3
BOOTSTRAP_REJECT_COOLDOWN_SECONDS = 30
BOOTSTRAP_INTENT_DESCRIPTION = "bootstrap"
BOOTSTRAP_INTENT_CREATOR = "dispatcher.bootstrap"
STARTUP_UNHEALTHY_RETRY_AFTER_SECONDS = 300


@dataclass(slots=True)
class WorkerSelection:
    worker: WorkerConfig | None
    blocked_phase_limit: str | None
    blocked_busy: list[str]
    blocked_unhealthy: list[str]
    blocked_rejected: list[str]
    blocked_task_type: list[str]
    blocked_pool: list[str]


@dataclass(slots=True)
class BootstrapRetryState:
    signature: str
    count: int
    blocked_until: float
    needs_hint: bool = False


class DispatcherLoop:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.config_snapshot = load_dispatch_config_snapshot(config_path)
        self.config_version = self.config_snapshot.version
        self.config = DispatchConfig.load(config_path)
        self.client = CairnClient(self.config.server)
        self.executor = ThreadPoolExecutor(max_workers=self.config.runtime.max_workers)
        self._retired_executors: list[ThreadPoolExecutor] = []
        self.futures: dict[Future[str], RunningTask] = {}
        self.reason_checkpoints: dict[str, ReasonCheckpoint] = {}
        self.runtime_project_ids: set[str] = set()
        self.worker_unhealthy_until: dict[str, float] = {}
        self.worker_rejected_until: dict[tuple[str, str, str], float] = {}
        self.bootstrap_retry_state: dict[tuple[str, str], BootstrapRetryState] = {}
        self._dispatch_event_cooldown: dict[tuple[str, str, str], float] = {}
        self._log_state: dict[str, tuple[int, str, tuple[object, ...]]] = {}
        self.project_cursor = 0
        self._settings_checked = False
        self._startup_healthchecks_checked = False
        self._publish_runtime_state(reason="startup")

    def close(self) -> None:
        if self.futures:
            LOG.info(
                "dispatcher shutting down waiting_for_tasks=%s running_projects=%s",
                len(self.futures),
                sorted({task.project_id for task in self.futures.values()}),
            )
        self.executor.shutdown(wait=True)
        for executor in self._retired_executors:
            executor.shutdown(wait=True)
        self.client.close()

    def run(self, once: bool = False) -> None:
        try:
            self.run_startup_healthchecks()
            while True:
                try:
                    if not self._settings_checked:
                        self._validate_server_settings()
                        self._settings_checked = True
                    self._refresh_dispatch_config_if_needed()
                    self._reap_futures()
                    summaries = self.client.list_projects()
                    if self._recover_stale_external_leases(summaries):
                        summaries = self.client.list_projects()
                    self._initialize_reason_checkpoints(summaries)
                    self._refresh_runtime_projects(summaries)
                    self._cancel_inactive_tasks(summaries)
                    self._dispatch_available(summaries)
                except requests.RequestException as exc:
                    if once:
                        raise
                    LOG.warning(
                        "dispatcher server request failed error=%s retry_in=%ss",
                        exc,
                        self.config.runtime.interval,
                    )
                    time.sleep(self.config.runtime.interval)
                    continue
                if once:
                    break
                time.sleep(self.config.runtime.interval)
        finally:
            self.close()

    def run_startup_healthchecks_only(self) -> None:
        try:
            self.run_startup_healthchecks(show_commands=True, force=True)
        finally:
            self.close()

    def run_startup_healthchecks(self, *, show_commands: bool = False, force: bool = False) -> None:
        if self._startup_healthchecks_checked:
            return
        if not force and self.config.runtime.worker_healthcheck == "disabled":
            LOG.info("skip startup worker healthchecks because runtime.worker_healthcheck=disabled")
            self._startup_healthchecks_checked = True
            return
        self._run_startup_healthchecks(show_commands=show_commands)
        self._startup_healthchecks_checked = True

    def _dispatch_available(self, summaries: list[ProjectSummary]) -> None:
        if len(self.futures) >= self.config.runtime.max_workers:
            LOG.debug(
                "skip dispatch because max_workers reached running_tasks=%s",
                len(self.futures),
            )
            return
        active = [summary for summary in summaries if summary.status == "active"]
        if not active:
            LOG.debug("skip dispatch because no active projects")
            return

        running_projects = self._ordered_projects(
            [summary for summary in active if summary.id in self.runtime_project_ids]
        )
        idle_projects = self._ordered_projects(
            [summary for summary in active if summary.id not in self.runtime_project_ids]
        )

        dispatched = True
        while dispatched and len(self.futures) < self.config.runtime.max_workers:
            dispatched = False
            for summary in running_projects:
                if self._try_dispatch_project(summary):
                    dispatched = True
                    if len(self.futures) >= self.config.runtime.max_workers:
                        return
            if dispatched:
                continue
            if self._running_project_count(active) >= self.config.runtime.max_running_projects:
                self._log_changed(
                    "dispatch/idle-limit",
                    logging.INFO,
                    "skip idle project dispatch because max_running_projects reached running_projects=%s",
                    self._running_project_count(active),
                )
                return
            for summary in idle_projects:
                if self._running_project_count(active) >= self.config.runtime.max_running_projects:
                    self._log_changed(
                        "dispatch/idle-limit",
                        logging.INFO,
                        "stop idle project dispatch because max_running_projects reached running_projects=%s",
                        self._running_project_count(active),
                    )
                    return
                if self._try_dispatch_project(summary):
                    dispatched = True
                    break

    def _ordered_projects(self, summaries: list[ProjectSummary]) -> list[ProjectSummary]:
        if not summaries:
            return []
        ids = [summary.id for summary in summaries]
        ids.sort()
        offset = self.project_cursor % len(ids)
        ordered_ids = ids[offset:] + ids[:offset]
        by_id = {summary.id: summary for summary in summaries}
        self.project_cursor += 1
        return [by_id[project_id] for project_id in ordered_ids]

    def _try_dispatch_project(self, summary: ProjectSummary) -> bool:
        skip_scope = f"project:{summary.id}:skip"
        if self._project_running_task_count(summary.id) >= self.config.runtime.max_project_workers:
            self._log_changed(
                f"{skip_scope}:max_project_workers",
                logging.INFO,
                "skip project=%s because max_project_workers reached running_tasks=%s",
                summary.id,
                self._project_running_task_summary(summary.id),
            )
            return False

        project = self.client.get_project(summary.id)
        if project.project.status != "active":
            self._log_changed(
                f"{skip_scope}:status",
                logging.INFO,
                "skip project=%s because status=%s",
                summary.id,
                project.project.status,
            )
            return False
        if self._is_initial_project(project):
            if project.project.reason is not None:
                return False
            if project.project.depth_mode:
                return self._dispatch_initial_project(project)
            if self._project_requires_bootstrap(project):
                return self._dispatch_initial_project(project)
            export_yaml = self.client.export_project(summary.id)
            return self._dispatch_reason(project, export_yaml, "initial")
        if project.project.reason is None:
            # Depth mode: re-dispatch plan if intent is unclaimed (survives restart)
            if project.project.depth_mode:
                unclaimed_plan = [i for i in project.intents
                                  if i.to is None and i.worker is None and self._is_plan_intent(i)]
                if unclaimed_plan:
                    return self._dispatch_initial_project(project)
                if self._depth_should_reason(project):
                    export_yaml = self.client.export_project(summary.id)
                    return self._dispatch_reason(project, export_yaml, "progress_check")
            else:
                reason_trigger = self._reason_trigger(project)
                if reason_trigger is not None:
                    # Don't start reason while explore tasks are still running locally.
                    # Wait until all running explores finish so reason sees their facts.
                    running_intent_ids = self._project_running_explore_intents(summary.id)
                    if running_intent_ids:
                        self._log_changed(
                            f"{skip_scope}:reason_wait_explore",
                            logging.DEBUG,
                            "defer reason project=%s trigger=%s waiting for %d explore(s) to finish",
                            summary.id,
                            reason_trigger,
                            len(running_intent_ids),
                        )
                        return False
                    export_yaml = self.client.export_project(summary.id)
                    return self._dispatch_reason(project, export_yaml, reason_trigger)
        running_intent_ids = self._project_running_explore_intents(summary.id)
        unclaimed_intents = [
            intent
            for intent in project.intents
            if self._is_open_intent(intent)
            and intent.worker is None
            and intent.id not in running_intent_ids
            and not self._is_bootstrap_intent(intent)
            and not self._is_plan_intent(intent)
        ]
        if running_intent_ids and not unclaimed_intents:
            self._log_changed(
                f"{skip_scope}:explore_running",
                logging.DEBUG,
                "skip explore project=%s because all unclaimed intents are already running locally intents=%s",
                summary.id,
                sorted(running_intent_ids),
            )
        if unclaimed_intents:
            oldest = min(unclaimed_intents, key=lambda i: (i.created_at, i.id))
            export_yaml = self.client.export_project(summary.id)
            return self._dispatch_explore(project, export_yaml, oldest)
        if project.project.reason is not None:
            self._log_changed(
                f"{skip_scope}:reason_claimed",
                logging.DEBUG,
                "skip reason project=%s because reason is already claimed by %s",
                summary.id,
                project.project.reason.worker,
            )
            return False
        self._log_changed(
            f"{skip_scope}:graph_unchanged",
            logging.DEBUG,
            "skip reason project=%s because reason state unchanged facts=%s hints=%s open_intents=%s intents=%s",
            summary.id,
            len(project.facts),
            len(project.hints),
            self._project_open_intent_count(project),
            len(project.intents),
        )
        return False

    def _dispatch_initial_project(self, project: ProjectDetail) -> bool:
        if project.project.depth_mode:
            return self._dispatch_plan_initial(project)
        return self._dispatch_bootstrap_initial(project)

    def _dispatch_bootstrap_initial(self, project: ProjectDetail) -> bool:
        intent = self._get_bootstrap_intent(project)
        if intent is None:
            intent = self._create_bootstrap_intent(project)
            if intent is None:
                return False
        if self._bootstrap_retry_blocked(project, intent):
            return False
        if self._project_has_running_bootstrap(project.project.id):
            self._log_changed(
                f"project:{project.project.id}:skip:bootstrap_running",
                logging.DEBUG,
                "skip bootstrap project=%s because bootstrap task is already running locally",
                project.project.id,
            )
            return False
        if intent.worker is not None:
            self._log_changed(
                f"project:{project.project.id}:skip:bootstrap_claimed",
                logging.DEBUG,
                "skip bootstrap project=%s because bootstrap intent=%s is already claimed by %s",
                project.project.id,
                intent.id,
                intent.worker,
            )
            return False
        return self._dispatch_bootstrap(project, intent)

    def _dispatch_plan_initial(self, project: ProjectDetail) -> bool:
        intent = self._get_plan_intent(project)
        if intent is None:
            intent = self._create_plan_intent(project)
            if intent is None:
                LOG.warning("failed to create plan intent project=%s", project.project.id)
                return False
            LOG.info("created plan intent project=%s intent=%s", project.project.id, intent.id)
        if self._project_has_running_plan(project.project.id):
            return False
        if intent.worker is not None:
            return False
        return self._dispatch_plan(project, intent)

    def _get_plan_intent(self, project: ProjectDetail) -> Intent | None:
        for intent in project.intents:
            if intent.creator == "dispatcher.plan" and intent.to is None:
                return intent
        return None

    def _create_plan_intent(self, project: ProjectDetail) -> Intent | None:
        pool = list(getattr(project.project, "worker_pool", []) or [])
        response = self.client.create_intent(
            project.project.id,
            ["origin"],
            "plan",
            "dispatcher.plan",
            targets=self._derive_system_intent_targets(project),
            worker_pool=pool,
        )
        if response.ok and isinstance(response.data, dict):
            intent_data = response.data
            return Intent(
                id=intent_data.get("id", ""),
                from_=intent_data.get("from", []),
                description=intent_data.get("description", ""),
                creator=intent_data.get("creator", ""),
                worker=intent_data.get("worker"),
                created_at=intent_data.get("created_at", ""),
                concluded_at=intent_data.get("concluded_at"),
                to=intent_data.get("to"),
            )
        return None

    def _depth_should_reason(self, project: ProjectDetail) -> bool:
        """Depth mode: trigger reason when all explores are done (idle)."""
        # Don't reason while plan is still running or claimed.
        if self._project_has_running_plan(project.project.id):
            return False
        # Also check if plan intent exists and is claimed (survives restart).
        for intent in project.intents:
            if self._is_plan_intent(intent) and intent.to is None and intent.worker is not None:
                return False
        open_intents = [i for i in project.intents if self._is_open_intent(i) and i.worker is None
                        and i.creator != "dispatcher.plan" and i.creator != "dispatcher.bootstrap"]
        running = self._project_running_explore_intents(project.project.id)
        if len(open_intents) > 0:
            return False
        # Allow reason to start when at most a few stragglers are still running.
        # This avoids waiting for the last slow explore before reasoning on the
        # majority of completed results. Threshold: ≤1 running (or ≤half of
        # max_project_workers, whichever is larger).
        straggler_threshold = max(1, self.config.runtime.max_project_workers // 2)
        return len(running) <= straggler_threshold

    def _project_has_running_plan(self, project_id: str) -> bool:
        return any(
            task.task_type == "plan" and task.project_id == project_id
            for task in self.futures.values()
        )

    def _dispatch_plan(self, project: ProjectDetail, intent: Intent) -> bool:
        selection = self._select_worker(project.project.id, "plan", worker_pool=getattr(intent, "worker_pool", None))
        if selection is None:
            self._log_changed(
                f"project:{project.project.id}:skip:plan_worker",
                logging.DEBUG,
                "no worker available for plan project=%s intent=%s",
                project.project.id,
                intent.id,
            )
            return False
        worker = selection.worker
        claim = self.client.heartbeat(project.project.id, intent.id, worker.name)
        if not claim.ok:
            return False
        try:
            cancellation = TaskCancellation()
            future = self.executor.submit(
                run_plan_task,
                self.config,
                self.client,
                project,
                intent,
                worker,
                cancellation,
            )
            self.futures[future] = RunningTask(
                project_id=project.project.id,
                task_type="plan",
                intent_id=intent.id,
                worker_name=worker.name,
                cancellation=cancellation,
                bootstrap_signature=None,
                fact_count=len(project.facts),
                hint_count=len(project.hints),
                open_intent_count=self._project_open_intent_count(project),
            )
            LOG.info("dispatched plan project=%s intent=%s worker=%s", project.project.id, intent.id, worker.name)
            return True
        except Exception:
            LOG.exception("failed to submit plan task project=%s intent=%s worker=%s", project.project.id, intent.id, worker.name)
            self._best_effort_release(project.project.id, intent.id, worker.name)
            return False

    def _dispatch_reason(self, project: ProjectDetail, export_yaml: str, trigger: str) -> bool:
        selection = self._select_worker(
            project.project.id,
            "reason",
            worker_pool=getattr(project.project, "worker_pool", None),
        )
        worker = selection.worker
        if worker is None:
            self._log_changed(
                f"project:{project.project.id}:worker:reason",
                logging.INFO,
                "no worker available for reason project=%s blocked_phase_limit=%s blocked_busy=%s blocked_unhealthy=%s blocked_rejected=%s",
                project.project.id,
                selection.blocked_phase_limit,
                selection.blocked_busy,
                selection.blocked_unhealthy,
                selection.blocked_rejected,
            )
            return False
        self._clear_log_state(f"project:{project.project.id}:worker:reason")
        claim = self.client.claim_reason(project.project.id, worker.name, trigger)
        if claim.status_code in (403, 409):
            level = logging.INFO if claim.status_code == 403 else logging.WARNING
            LOG.log(
                level,
                "reason claim failed project=%s worker=%s status=%s",
                project.project.id,
                worker.name,
                claim.status_code,
            )
            return False
        if not claim.ok:
            LOG.warning(
                "reason claim failed project=%s worker=%s status=%s",
                project.project.id,
                worker.name,
                claim.status_code,
            )
            return False
        try:
            future = self.executor.submit(
                run_reason_task,
                self.config,
                self.client,
                project,
                export_yaml,
                worker,
                cancellation := TaskCancellation(),
            )
        except Exception:
            LOG.exception("failed to submit reason task project=%s worker=%s", project.project.id, worker.name)
            self._best_effort_release_reason(project.project.id, worker.name)
            return False
        self.futures[future] = RunningTask(
            project.project.id,
            "reason",
            worker.name,
            cancellation,
            intent_id=None,
            fact_count=len(project.facts),
            hint_count=len(project.hints),
            open_intent_count=self._project_open_intent_count(project),
        )
        self.runtime_project_ids.add(project.project.id)
        self._clear_project_log_state(project.project.id)
        LOG.info("dispatched reason project=%s worker=%s trigger=%s", project.project.id, worker.name, trigger)
        return True

    def _dispatch_bootstrap(self, project: ProjectDetail, intent: Intent) -> bool:
        selection = self._select_worker(project.project.id, "bootstrap", worker_pool=getattr(intent, "worker_pool", None))
        worker = selection.worker
        if worker is None:
            self._log_changed(
                f"project:{project.project.id}:worker:bootstrap",
                logging.INFO,
                "no worker available for bootstrap project=%s intent=%s blocked_phase_limit=%s blocked_busy=%s blocked_unhealthy=%s blocked_rejected=%s",
                project.project.id,
                intent.id,
                selection.blocked_phase_limit,
                selection.blocked_busy,
                selection.blocked_unhealthy,
                selection.blocked_rejected,
            )
            return False
        self._clear_log_state(f"project:{project.project.id}:worker:bootstrap")
        claim = self.client.heartbeat(project.project.id, intent.id, worker.name)
        if claim.status_code in (403, 409):
            level = logging.INFO if claim.status_code == 403 else logging.WARNING
            LOG.log(
                level,
                "bootstrap claim failed project=%s intent=%s worker=%s status=%s",
                project.project.id,
                intent.id,
                worker.name,
                claim.status_code,
            )
            return False
        if not claim.ok:
            LOG.warning(
                "bootstrap claim failed project=%s intent=%s worker=%s status=%s",
                project.project.id,
                intent.id,
                worker.name,
                claim.status_code,
            )
            return False
        try:
            future = self.executor.submit(
                run_bootstrap_task,
                self.config,
                self.client,
                project,
                intent,
                worker,
                cancellation := TaskCancellation(),
            )
        except Exception:
            LOG.exception("failed to submit bootstrap task project=%s intent=%s worker=%s", project.project.id, intent.id, worker.name)
            self._best_effort_release(project.project.id, intent.id, worker.name)
            return False
        self.futures[future] = RunningTask(
            project.project.id,
            "bootstrap",
            worker.name,
            cancellation,
            intent_id=intent.id,
            bootstrap_signature=self._bootstrap_retry_signature(project),
        )
        self.runtime_project_ids.add(project.project.id)
        self._clear_project_log_state(project.project.id)
        LOG.info("dispatched bootstrap project=%s intent=%s worker=%s", project.project.id, intent.id, worker.name)
        return True

    def _dispatch_explore(self, project: ProjectDetail, export_yaml: str, intent: Intent) -> bool:
        selection = self._select_worker(project.project.id, "explore", worker_pool=getattr(intent, "worker_pool", None))
        worker = selection.worker
        if worker is None:
            self._log_changed(
                f"project:{project.project.id}:worker:explore",
                logging.INFO,
                "no worker available for explore project=%s intent=%s blocked_phase_limit=%s blocked_busy=%s blocked_unhealthy=%s blocked_rejected=%s",
                project.project.id,
                intent.id,
                selection.blocked_phase_limit,
                selection.blocked_busy,
                selection.blocked_unhealthy,
                selection.blocked_rejected,
            )
            self._report_dispatch_blocked(
                project_id=project.project.id,
                intent=intent,
                worker=selection.blocked_busy[0] if selection.blocked_busy else "dispatcher",
                reason="worker_unavailable",
                payload={
                    "blocked_phase_limit": selection.blocked_phase_limit,
                    "blocked_busy": selection.blocked_busy,
                    "blocked_unhealthy": selection.blocked_unhealthy,
                    "blocked_rejected": selection.blocked_rejected,
                },
            )
            return False
        self._clear_log_state(f"project:{project.project.id}:worker:explore")
        claim = self.client.heartbeat(project.project.id, intent.id, worker.name)
        if claim.status_code in (403, 409):
            level = logging.INFO if claim.status_code == 403 else logging.WARNING
            LOG.log(
                level,
                "explore claim failed project=%s intent=%s worker=%s status=%s",
                project.project.id,
                intent.id,
                worker.name,
                claim.status_code,
            )
            return False
        if not claim.ok:
            LOG.warning(
                "explore claim failed project=%s intent=%s worker=%s status=%s",
                project.project.id,
                intent.id,
                worker.name,
                claim.status_code,
            )
            return False
        try:
            future = self.executor.submit(
                run_explore_task,
                self.config,
                self.client,
                project,
                export_yaml,
                intent,
                worker,
                cancellation := TaskCancellation(),
            )
        except Exception:
            LOG.exception("failed to submit explore task project=%s intent=%s worker=%s", project.project.id, intent.id, worker.name)
            self._best_effort_release(project.project.id, intent.id, worker.name)
            return False
        self.futures[future] = RunningTask(
            project.project.id, "explore", worker.name, cancellation,
            intent_id=intent.id, intent_description=intent.description,
        )
        self.runtime_project_ids.add(project.project.id)
        self._clear_project_log_state(project.project.id)
        LOG.info("dispatched explore project=%s intent=%s worker=%s", project.project.id, intent.id, worker.name)
        return True

    def _select_worker(
        self,
        project_id: str,
        task_type: str,
        worker_pool: list[str] | None = None,
    ) -> WorkerSelection:
        now = time.time()
        candidates: list[WorkerConfig] = []
        blocked_phase_limit: str | None = None
        blocked_busy: list[str] = []
        blocked_unhealthy: list[str] = []
        blocked_rejected: list[str] = []
        blocked_task_type: list[str] = []
        blocked_pool: list[str] = []
        pool_set: set[str] | None = set(worker_pool) if worker_pool else None
        running_counts = self._worker_counts()
        phase_limit = self.config.phase_limits.for_task(task_type)
        running_for_phase = self._phase_running_count(task_type)
        if running_for_phase >= phase_limit:
            blocked_phase_limit = f"{running_for_phase}/{phase_limit}"
        for worker in self.config.workers:
            if worker.disabled:
                continue
            if task_type not in worker.task_types:
                blocked_task_type.append(worker.name)
                continue
            if pool_set is not None and worker.name not in pool_set:
                blocked_pool.append(worker.name)
                continue
            if blocked_phase_limit is not None:
                continue
            running = running_counts.get(worker.name, 0)
            if running >= worker.max_running:
                blocked_busy.append(f"{worker.name}({running}/{worker.max_running})")
                continue
            unhealthy_until = self.worker_unhealthy_until.get(worker.name, 0)
            if unhealthy_until > now:
                blocked_unhealthy.append(f"{worker.name}({unhealthy_until - now:.1f}s)")
                continue
            rejected_until = self.worker_rejected_until.get((project_id, task_type, worker.name), 0)
            if rejected_until > now:
                blocked_rejected.append(f"{worker.name}({rejected_until - now:.1f}s)")
                continue
            candidates.append(worker)
        if not candidates:
            LOG.debug(
                "worker selection project=%s task=%s no candidates blocked_phase_limit=%s blocked_busy=%s blocked_unhealthy=%s blocked_rejected=%s blocked_task_type=%s",
                project_id,
                task_type,
                blocked_phase_limit,
                blocked_busy,
                blocked_unhealthy,
                blocked_rejected,
                blocked_task_type,
            )
            return WorkerSelection(
                worker=None,
                blocked_phase_limit=blocked_phase_limit,
                blocked_busy=blocked_busy,
                blocked_unhealthy=blocked_unhealthy,
                blocked_rejected=blocked_rejected,
                blocked_task_type=blocked_task_type,
                blocked_pool=blocked_pool,
            )
        ordered = choose_worker(candidates, running_counts)
        LOG.debug(
            "worker selection project=%s task=%s candidates=%s blocked_phase_limit=%s blocked_busy=%s blocked_unhealthy=%s blocked_rejected=%s blocked_task_type=%s chosen=%s",
            project_id,
            task_type,
            [f"{worker.name}({running_counts.get(worker.name, 0)}/{worker.max_running},p{worker.priority})" for worker in candidates],
            blocked_phase_limit,
            blocked_busy,
            blocked_unhealthy,
            blocked_rejected,
            blocked_task_type,
            ordered[0].name if ordered else None,
        )
        return WorkerSelection(
            worker=ordered[0] if ordered else None,
            blocked_phase_limit=blocked_phase_limit,
            blocked_busy=blocked_busy,
            blocked_unhealthy=blocked_unhealthy,
            blocked_rejected=blocked_rejected,
            blocked_task_type=blocked_task_type,
            blocked_pool=blocked_pool,
        )

    def _worker_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for task in self.futures.values():
            counts[task.worker_name] = counts.get(task.worker_name, 0) + 1
        return counts

    def _phase_running_count(self, task_type: str) -> int:
        return sum(1 for task in self.futures.values() if task.task_type == task_type)

    def _project_running_task_count(self, project_id: str) -> int:
        return sum(1 for task in self.futures.values() if task.project_id == project_id)

    def _project_running_task_summary(self, project_id: str) -> list[str]:
        summary: list[str] = []
        for task in self.futures.values():
            if task.project_id != project_id:
                continue
            if task.intent_id is None:
                summary.append(f"{task.task_type}:{task.worker_name}")
            else:
                summary.append(f"{task.task_type}:{task.worker_name}:{task.intent_id}")
        summary.sort()
        return summary

    def _project_has_running_bootstrap(self, project_id: str) -> bool:
        return any(task.project_id == project_id and task.task_type == "bootstrap" for task in self.futures.values())

    def _project_running_explore_intents(self, project_id: str) -> set[str]:
        return {
            task.intent_id
            for task in self.futures.values()
            if task.project_id == project_id and task.task_type == "explore" and task.intent_id is not None
        }

    def _running_project_count(self, summaries: list[ProjectSummary]) -> int:
        active_ids = {summary.id for summary in summaries if summary.status == "active"}
        return len(self.runtime_project_ids & active_ids)

    def _project_open_intent_count(self, project: ProjectDetail) -> int:
        return sum(1 for intent in project.intents if self._is_open_intent(intent))

    @staticmethod
    def _is_open_intent(intent: Intent) -> bool:
        return intent.to is None and intent.concluded_at is None

    def _is_plan_intent(self, intent: Intent) -> bool:
        return intent.creator == "dispatcher.plan"

    def _is_bootstrap_intent(self, intent: Intent) -> bool:
        return (
            intent.description == BOOTSTRAP_INTENT_DESCRIPTION
            and intent.creator == BOOTSTRAP_INTENT_CREATOR
            and intent.from_ == ["origin"]
            and intent.to is None
        )

    def _get_bootstrap_intent(self, project: ProjectDetail) -> Intent | None:
        intents = [intent for intent in project.intents if self._is_bootstrap_intent(intent)]
        if not intents:
            return None
        if len(intents) > 1:
            LOG.warning("project has multiple bootstrap intents project=%s intents=%s", project.project.id, [intent.id for intent in intents])
        intents.sort(key=lambda intent: (intent.worker is not None, intent.created_at, intent.id))
        return intents[0]

    def _is_initial_project(self, project: ProjectDetail) -> bool:
        fact_ids = {fact.id for fact in project.facts}
        if fact_ids != {"origin", "goal"} or len(project.facts) != 2:
            return False
        if not project.intents:
            return True
        return all(self._is_bootstrap_intent(intent) for intent in project.intents)

    def _project_requires_bootstrap(self, project: ProjectDetail) -> bool:
        if not project.project.bootstrap_enabled:
            return False
        if self._get_bootstrap_intent(project) is not None:
            return True
        return any("bootstrap" in worker.task_types for worker in self.config.workers)

    def _create_bootstrap_intent(self, project: ProjectDetail) -> Intent | None:
        pool = list(getattr(project.project, "worker_pool", []) or [])
        response = self.client.create_intent(
            project.project.id,
            ["origin"],
            BOOTSTRAP_INTENT_DESCRIPTION,
            BOOTSTRAP_INTENT_CREATOR,
            targets=self._derive_system_intent_targets(project),
            worker_pool=pool,
        )
        if response.status_code == 403:
            LOG.info("project became inactive before bootstrap intent create project=%s", project.project.id)
            return None
        if not response.ok:
            LOG.warning(
                "bootstrap intent write failed project=%s status=%s body=%s",
                project.project.id,
                response.status_code,
                response.text,
            )
            return None
        if not isinstance(response.data, dict):
            LOG.warning("bootstrap intent create returned empty body project=%s", project.project.id)
            return None
        intent = Intent.model_validate(response.data)
        LOG.info("created bootstrap intent project=%s intent=%s", project.project.id, intent.id)
        return intent

    def _derive_system_intent_targets(self, project: ProjectDetail) -> list[str]:
        def extract_host(text: str) -> str | None:
            value = (text or "").strip()
            if not value:
                return None
            if "://" in value:
                host = (urlparse(value).hostname or "").strip().lower()
                return host or None
            candidate = value.split("/", 1)[0].split(":", 1)[0].strip().strip(".").lower()
            return candidate or None

        candidates: list[str] = []
        for fact in project.facts:
            if fact.id != "origin":
                continue
            for token in str(fact.description).replace("\n", " ").split():
                host = extract_host(token)
                if host:
                    candidates.append(host)
        seen: set[str] = set()
        targets: list[str] = []
        for item in candidates:
            if item in seen:
                continue
            seen.add(item)
            targets.append(item)
        if targets:
            return targets
        return ["127.0.0.1"]

    def _bootstrap_retry_signature(self, project: ProjectDetail) -> str:
        hint_ids = ",".join(sorted(hint.id for hint in project.hints))
        fact_ids = ",".join(sorted(fact.id for fact in project.facts))
        return f"facts:{fact_ids}|hints:{hint_ids}"

    def _bootstrap_retry_blocked(self, project: ProjectDetail, intent: Intent) -> bool:
        state = self.bootstrap_retry_state.get((project.project.id, intent.id))
        if state is None:
            return False
        signature = self._bootstrap_retry_signature(project)
        if state.signature != signature:
            self.bootstrap_retry_state.pop((project.project.id, intent.id), None)
            return False
        now = time.time()
        if state.blocked_until > now:
            self._log_changed(
                f"project:{project.project.id}:skip:bootstrap_backoff",
                logging.INFO,
                "skip bootstrap project=%s intent=%s due_to=retry_backoff remaining=%.1fs count=%s",
                project.project.id,
                intent.id,
                state.blocked_until - now,
                state.count,
            )
            return True
        if state.needs_hint:
            self._log_changed(
                f"project:{project.project.id}:skip:bootstrap_needs_hint",
                logging.WARNING,
                "skip bootstrap project=%s intent=%s due_to=needs_hint count=%s signature=%s",
                project.project.id,
                intent.id,
                state.count,
                signature,
            )
            return True
        return False

    def _reason_trigger(self, project: ProjectDetail) -> str | None:
        open_intent_count = self._project_open_intent_count(project)
        checkpoint = self.reason_checkpoints.get(project.project.id)
        if checkpoint is None:
            return "initial"
        changes: list[str] = []
        if len(project.facts) > checkpoint.fact_count:
            changes.append(f"facts:{checkpoint.fact_count}->{len(project.facts)}")
        if len(project.hints) > checkpoint.hint_count:
            changes.append(f"hints:{checkpoint.hint_count}->{len(project.hints)}")
        if checkpoint.open_intent_count > 0 and open_intent_count == 0:
            changes.append(f"open_intents:{checkpoint.open_intent_count}->0")
        # Stale: all open intents done but facts haven't changed — no other
        # trigger will fire.  Re-run reason after a cooldown to let the model
        # either create new intents or mark the project complete.
        if open_intent_count == 0 and not changes:
            now = time.time()
            if now - checkpoint.last_triggered_at >= self.config.tasks.reason.timeout:
                changes.append("stale")
        if not changes:
            return None
        return ",".join(changes)

    def _update_bootstrap_retry_state(self, task: RunningTask, outcome: str) -> None:
        key = (task.project_id, task.intent_id)
        assert task.intent_id is not None
        if outcome == "api_error":
            # API-level errors (connection/auth failures) affect all workers
            # equally.  Block retries for a while to avoid tight cycling.
            self.bootstrap_retry_state[key] = BootstrapRetryState(
                signature=task.bootstrap_signature or "",
                count=1,
                blocked_until=time.time() + BOOTSTRAP_REJECT_COOLDOWN_SECONDS,
                needs_hint=False,
            )
            return
        if outcome != "rejected":
            self.bootstrap_retry_state.pop(key, None)
            return
        previous = self.bootstrap_retry_state.get(key)
        same_signature = previous is not None and previous.signature == task.bootstrap_signature
        count = (previous.count + 1) if same_signature else 1
        needs_hint = count >= BOOTSTRAP_REJECT_STALL_THRESHOLD
        blocked_until = time.time() + BOOTSTRAP_REJECT_COOLDOWN_SECONDS
        self.bootstrap_retry_state[key] = BootstrapRetryState(
            signature=task.bootstrap_signature or "",
            count=count,
            blocked_until=blocked_until,
            needs_hint=needs_hint,
        )
        if needs_hint:
            LOG.warning(
                "bootstrap stalled project=%s intent=%s worker=%s count=%s until=%.0fs",
                task.project_id,
                task.intent_id,
                task.worker_name,
                count,
                BOOTSTRAP_REJECT_COOLDOWN_SECONDS,
            )
        else:
            LOG.info(
                "bootstrap retry backoff project=%s intent=%s worker=%s count=%s until=%.0fs",
                task.project_id,
                task.intent_id,
                task.worker_name,
                count,
                BOOTSTRAP_REJECT_COOLDOWN_SECONDS,
            )

    def _refresh_dispatch_config_if_needed(self) -> None:
        version = peek_dispatch_config_version()
        if version.version <= 0 or version.version == self.config_version.version:
            return
        snapshot = load_dispatch_config_snapshot(self.config_path)
        if snapshot.version.version == self.config_version.version:
            return
        previous_workers = {worker.name for worker in self.config.workers}
        previous_max_workers = self.config.runtime.max_workers
        self.config_snapshot = snapshot
        self.config_version = snapshot.version
        self.config = DispatchConfig.load(self.config_path)
        self._settings_checked = False
        self._replace_executors_if_needed(previous_max_workers)
        self._clear_removed_worker_state(previous_workers)
        self._publish_runtime_state(reason="reload")
        LOG.info(
            "dispatcher config reloaded version=%s updated_at=%s workers=%s phase_limits=%s",
            self.config_version.version,
            self.config_version.updated_at,
            [worker.name for worker in self.config.workers],
            {
                "bootstrap": self.config.phase_limits.bootstrap,
                "explore": self.config.phase_limits.explore,
                "reason": self.config.phase_limits.reason,
            },
        )

    def _replace_executors_if_needed(self, previous_max_workers: int) -> None:
        if previous_max_workers == self.config.runtime.max_workers:
            return
        self._retired_executors.append(self.executor)
        self.executor = ThreadPoolExecutor(max_workers=self.config.runtime.max_workers)

    def _clear_removed_worker_state(self, previous_workers: set[str]) -> None:
        current_workers = {worker.name for worker in self.config.workers}
        removed_workers = previous_workers - current_workers
        if not removed_workers:
            return
        for worker_name in removed_workers:
            self.worker_unhealthy_until.pop(worker_name, None)
        for key in list(self.worker_rejected_until):
            if key[2] in removed_workers:
                self.worker_rejected_until.pop(key, None)

    def _publish_runtime_state(self, *, reason: str) -> None:
        write_dispatcher_runtime_state(
            config_version=self.config_version.version,
            workers=[
                {
                    "name": worker.name,
                    "task_types": list(worker.task_types),
                    "max_running": worker.max_running,
                    "priority": worker.priority,
                    "disabled": worker.disabled,
                }
                for worker in self.config.workers
            ],
            metadata={
                "reason": reason,
                "interval": self.config.runtime.interval,
                "max_workers": self.config.runtime.max_workers,
                "max_running_projects": self.config.runtime.max_running_projects,
                "max_project_workers": self.config.runtime.max_project_workers,
                "phase_limits": {
                    "bootstrap": self.config.phase_limits.bootstrap,
                    "explore": self.config.phase_limits.explore,
                    "reason": self.config.phase_limits.reason,
                },
            },
        )

    def _reap_futures(self) -> None:
        done = [future for future in self.futures if future.done()]
        for future in done:
            task = self.futures.pop(future)
            try:
                outcome = future.result()
                if outcome == "cancelled":
                    LOG.info(
                        "task cancelled project=%s task=%s worker=%s",
                        task.project_id,
                        task.task_type,
                        task.worker_name,
                    )
                elif outcome != "success":
                    LOG.warning(
                        "task finished project=%s task=%s worker=%s outcome=%s",
                        task.project_id,
                        task.task_type,
                        task.worker_name,
                        outcome,
                    )
                    self._report_task_outcome(task, outcome)
                self._clear_project_log_state(task.project_id)
                if outcome == "unhealthy":
                    retry_after_seconds = UNHEALTHY_RETRY_AFTER_SECONDS
                    self.worker_unhealthy_until[task.worker_name] = time.time() + retry_after_seconds
                    LOG.info(
                        "worker marked unhealthy worker=%s retry_after=%.0fs",
                        task.worker_name,
                        retry_after_seconds,
                    )
                elif outcome in {"hard_timeout_execute", "hard_timeout_conclude", "hard_timeout_both"}:
                    retry_after_seconds = 60
                    self.worker_unhealthy_until[task.worker_name] = time.time() + retry_after_seconds
                    LOG.info(
                        "worker marked hard_timeout worker=%s retry_after=%.0fs outcome=%s",
                        task.worker_name,
                        retry_after_seconds,
                        outcome,
                    )
                elif outcome == "parse_failure":
                    retry_after_seconds = 10
                    self.worker_rejected_until[(task.project_id, task.task_type, task.worker_name)] = time.time() + retry_after_seconds
                    LOG.info(
                        "worker marked parse_failure project=%s task=%s worker=%s retry_after=%.0fs",
                        task.project_id,
                        task.task_type,
                        task.worker_name,
                        retry_after_seconds,
                    )
                elif outcome == "needs_replan":
                    self._handle_needs_replan(task)
                else:
                    self.worker_unhealthy_until.pop(task.worker_name, None)
                rejection_key = (task.project_id, task.task_type, task.worker_name)
                if outcome == "rejected":
                    retry_after_seconds = REJECTED_RETRY_AFTER_SECONDS
                    self.worker_rejected_until[rejection_key] = time.time() + retry_after_seconds
                    LOG.info(
                        "worker marked rejected project=%s task=%s worker=%s retry_after=%.0fs",
                        task.project_id,
                        task.task_type,
                        task.worker_name,
                        retry_after_seconds,
                    )
                else:
                    self.worker_rejected_until.pop(rejection_key, None)
                if task.task_type == "bootstrap" and task.intent_id is not None:
                    self._update_bootstrap_retry_state(task, outcome)
                if outcome == "success" and task.task_type == "explore" and task.intent_id is not None:
                    self._enqueue_maintainer(task.project_id, task.intent_id)
                if outcome == "success" and task.task_type == "reason":
                    assert task.fact_count is not None
                    assert task.hint_count is not None
                    assert task.open_intent_count is not None
                    self.reason_checkpoints[task.project_id] = ReasonCheckpoint(
                        fact_count=task.fact_count,
                        hint_count=task.hint_count,
                        open_intent_count=task.open_intent_count,
                        last_triggered_at=time.time(),
                    )
                    LOG.debug(
                        "reason checkpoint updated project=%s facts=%s hints=%s open_intents=%s",
                        task.project_id,
                        task.fact_count,
                        task.hint_count,
                        task.open_intent_count,
                    )
            except Exception:
                LOG.exception("task crashed project=%s task=%s worker=%s", task.project_id, task.task_type, task.worker_name)

    def _handle_needs_replan(self, task) -> None:
        """Handle an explore task that exceeded max retries.
        Conclude the intent with a failure note so it won't be re-dispatched."""
        if task.intent_id is None:
            return

        try:
            self.client.conclude(
                task.project_id,
                task.intent_id,
                task.worker_name,
                description=f"超时重试 {MAX_EXPLORE_RETRIES} 次未完成。",
            )
        except Exception:
            LOG.exception("failed to conclude replanned intent project=%s intent=%s", task.project_id, task.intent_id)

    def _refresh_runtime_projects(self, summaries: list[ProjectSummary]) -> None:
        active_ids = {summary.id for summary in summaries if summary.status == "active"}
        self.runtime_project_ids.intersection_update(active_ids)

    def _recover_stale_external_leases(self, summaries: list[ProjectSummary]) -> bool:
        recovered = False
        for summary in summaries:
            if summary.status != "active":
                continue
            if summary.reason is None and summary.working_intent_count == 0:
                continue
            if self._project_running_task_count(summary.id) > 0:
                continue
            try:
                project = self.client.get_project(summary.id)
            except requests.RequestException:
                raise
            except Exception:
                LOG.exception("failed to inspect stale leases project=%s", summary.id)
                continue
            reason = project.project.reason
            if reason is not None and self._lease_timestamp_stale(reason.last_heartbeat_at):
                response = self.client.release_reason(project.project.id, reason.worker)
                if response.ok or response.status_code in (403, 409):
                    recovered = response.ok or recovered
                    LOG.warning(
                        "released stale external reason lease project=%s worker=%s last_heartbeat_at=%s status=%s",
                        project.project.id,
                        reason.worker,
                        reason.last_heartbeat_at,
                        response.status_code,
                    )
                else:
                    LOG.warning(
                        "failed to release stale external reason lease project=%s worker=%s status=%s",
                        project.project.id,
                        reason.worker,
                        response.status_code,
                    )
            running_intents = self._project_running_intent_ids(project.project.id)
            for intent in project.intents:
                if not self._is_open_intent(intent):
                    continue
                if intent.worker is None or intent.id in running_intents:
                    continue
                timestamp = intent.last_heartbeat_at or intent.created_at
                if not self._lease_timestamp_stale(timestamp):
                    continue
                response = self.client.release(project.project.id, intent.id, intent.worker)
                if response.ok or response.status_code in (403, 409):
                    recovered = response.ok or recovered
                    LOG.warning(
                        "released stale external intent lease project=%s intent=%s worker=%s last_heartbeat_at=%s status=%s",
                        project.project.id,
                        intent.id,
                        intent.worker,
                        intent.last_heartbeat_at,
                        response.status_code,
                    )
                else:
                    LOG.warning(
                        "failed to release stale external intent lease project=%s intent=%s worker=%s status=%s",
                        project.project.id,
                        intent.id,
                        intent.worker,
                        response.status_code,
                    )
        return recovered

    def _project_running_intent_ids(self, project_id: str) -> set[str]:
        return {
            task.intent_id
            for task in self.futures.values()
            if task.project_id == project_id and task.intent_id is not None
        }

    def _lease_timestamp_stale(self, value: str | None) -> bool:
        if not value:
            return False
        try:
            timestamp = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            LOG.warning("could not parse lease timestamp value=%s", value)
            return False
        grace_seconds = max(30, self.config.runtime.interval * 4)
        return (datetime.now(timezone.utc) - timestamp).total_seconds() > grace_seconds

    def _enqueue_maintainer(self, project_id: str, intent_id: str) -> None:
        """Submit a maintainer task to scan findings after worker success."""
        worker = next((w for w in self.config.workers if w.name == "stepfun-flash" and not w.disabled), None)
        if worker is None:
            return
        parent = ""
        try:
            summary = self.client.get_project(project_id)
            parent = summary.project.parent_project_id or ""
        except Exception:
            pass
        findings_path = f"/tmp/axiom-files/{parent or 'unknown'}/{project_id}/ai/{intent_id}_findings.md"
        if not Path(findings_path).exists():
            LOG.info("maintainer skipped: findings file not found project=%s intent=%s", project_id, intent_id)
            return
        try:
            project = self.client.get_project(project_id)
            export = self.client.export_project(project_id)
            cancellation = TaskCancellation()
            future = self.executor.submit(
                run_maintainer_task,
                self.config,
                self.client,
                worker,
                project,
                export,
                cancellation,
                {"intent_id": intent_id, "findings_path": findings_path, "group_id": parent},
            )
            self.futures[future] = RunningTask(project_id, "maintainer", worker.name, cancellation, intent_id=intent_id)
            LOG.info("maintainer submitted project=%s intent=%s", project_id, intent_id)
        except Exception:
            LOG.exception("maintainer submit failed project=%s intent=%s", project_id, intent_id)

    def _cancel_inactive_tasks(self, summaries: list[ProjectSummary]) -> None:
        status_by_project = {summary.id: summary.status for summary in summaries}
        for task in self.futures.values():
            status = status_by_project.get(task.project_id, "deleted")
            if status != "active" and task.cancellation.cancel(status):
                LOG.info(
                    "cancelling running task for inactive project project=%s task=%s worker=%s status=%s",
                    task.project_id,
                    task.task_type,
                    task.worker_name,
                    status,
                )

    def _initialize_reason_checkpoints(self, summaries: list[ProjectSummary]) -> None:
        for summary in summaries:
            if summary.status != "active":
                continue
            if summary.id in self.reason_checkpoints:
                continue
            open_intent_count = summary.working_intent_count + summary.unclaimed_intent_count
            if open_intent_count == 0:
                continue
            self.reason_checkpoints[summary.id] = ReasonCheckpoint(
                fact_count=summary.fact_count,
                hint_count=summary.hint_count,
                open_intent_count=open_intent_count,
            )
            LOG.debug(
                "reason checkpoint initialized project=%s facts=%s hints=%s open_intents=%s",
                summary.id,
                summary.fact_count,
                summary.hint_count,
                open_intent_count,
            )

    def _best_effort_release(self, project_id: str, intent_id: str, worker_name: str) -> None:
        response = self.client.release(project_id, intent_id, worker_name)
        if not response.ok and response.status_code not in (403, 409):
            LOG.warning("release failed project=%s intent=%s worker=%s status=%s", project_id, intent_id, worker_name, response.status_code)

    def _best_effort_release_reason(self, project_id: str, worker_name: str) -> None:
        response = self.client.release_reason(project_id, worker_name)
        if not response.ok and response.status_code not in (403, 409):
            LOG.warning("reason release failed project=%s worker=%s status=%s", project_id, worker_name, response.status_code)

    def _log_changed(self, scope: str, level: int, message: str, *args: object) -> None:
        state = (level, message, args)
        if self._log_state.get(scope) == state:
            return
        self._log_state[scope] = state
        LOG.log(level, message, *args)

    def _report_dispatch_blocked(
        self,
        *,
        project_id: str,
        intent: Intent,
        worker: str,
        reason: str,
        payload: dict[str, object] | None = None,
    ) -> None:
        cooldown = getattr(self, "_dispatch_event_cooldown", {})
        now = time.time()
        key = (project_id, intent.id, reason)
        if cooldown.get(key, 0) > now:
            return
        cooldown[key] = now + 30
        self._dispatch_event_cooldown = cooldown
        event_payload = {
            "intent_id": intent.id,
            "intent_description": intent.description,
            "reason": reason,
        }
        if payload:
            event_payload.update(payload)
        try:
            self.client.report_event(
                project_id,
                task_type="dispatcher",
                phase="dispatch_explore",
                worker=worker,
                level="warning",
                message="intent blocked_unassigned",
                payload=event_payload,
            )
        except Exception:
            LOG.exception("failed to report dispatch blocked event project=%s intent=%s", project_id, intent.id)

    def _report_task_outcome(self, task: RunningTask, outcome: str) -> None:
        payload: dict[str, object] = {"outcome": outcome}
        if task.intent_id is not None:
            payload["intent_id"] = task.intent_id
            if task.intent_description:
                payload["intent_description"] = task.intent_description
        level = "warning" if outcome not in {"cancelled", "rejected"} else "info"
        try:
            self.client.report_event(
                task.project_id,
                task_type=task.task_type,
                phase="task_outcome",
                worker=task.worker_name,
                level=level,
                message="task outcome",
                payload=payload,
            )
        except Exception:
            LOG.exception(
                "failed to report task outcome project=%s task=%s worker=%s outcome=%s",
                task.project_id,
                task.task_type,
                task.worker_name,
                outcome,
            )

    def _clear_log_state(self, scope: str) -> None:
        self._log_state.pop(scope, None)

    def _clear_project_log_state(self, project_id: str) -> None:
        prefix = f"project:{project_id}:"
        for scope in list(self._log_state):
            if scope.startswith(prefix):
                self._log_state.pop(scope, None)

    def _validate_server_settings(self) -> None:
        settings = self.client.get_settings()
        interval = self.config.runtime.interval
        for name, value in (("intent_timeout", settings.intent_timeout), ("reason_timeout", settings.reason_timeout)):
            if value <= interval:
                raise RuntimeError(
                    f"server {name}={value}s must be greater than dispatcher interval={interval}s"
                )
            if value < interval * 2:
                LOG.warning(
                    "server %s is tight %s=%ss interval=%ss; heartbeat slack is only %ss",
                    name,
                    name,
                    value,
                    interval,
                    value - interval,
                )
                continue
            LOG.info(
                "server setting validated %s=%ss interval=%ss",
                name,
                value,
                interval,
            )

    def _run_startup_healthchecks(self, *, show_commands: bool) -> None:
        results = run_startup_healthchecks(self.config, show_commands=show_commands)
        now = time.time()
        for result in results:
            if result.ok:
                self.worker_unhealthy_until.pop(result.worker_name, None)
                continue
            self.worker_unhealthy_until[result.worker_name] = now + STARTUP_UNHEALTHY_RETRY_AFTER_SECONDS
            LOG.warning(
                "worker marked unhealthy from startup check worker=%s retry_after=%.0fs",
                result.worker_name,
                STARTUP_UNHEALTHY_RETRY_AFTER_SECONDS,
            )
        if any(result.ok for result in results):
            return
        raise RuntimeError(format_failure_summary(results))
