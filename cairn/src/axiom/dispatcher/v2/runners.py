from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from axiom.dispatcher.config import WorkerConfig
from axiom.dispatcher.prompting import load_prompt, render_prompt
from axiom.dispatcher.runtime.process import ManagedProcess, ProcessResult
from axiom.dispatcher.v2.client import V2Client
from axiom.dispatcher.v2.schemas import DispatchAction, WorkerSnapshot
from axiom.dispatcher.workers.registry import get_driver
from axiom.modules.settings.schemas import RuntimeConfig


LOG = logging.getLogger(__name__)


class RunnerFailure(RuntimeError):
    pass


class LeaseHeartbeat:
    def __init__(self, callback: Callable[[], object], *, interval: int):
        self.callback = callback
        self.interval = interval
        self.stop_event = threading.Event()
        self.process: ManagedProcess | None = None
        self.failure: Exception | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self.callback()
        self.thread.start()

    def attach(self, process: ManagedProcess | None) -> None:
        self.process = process

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=1)

    def _run(self) -> None:
        while not self.stop_event.wait(self.interval):
            try:
                self.callback()
            except Exception as exc:
                self.failure = exc
                if self.process is not None:
                    self.process.kill()
                return


class PhaseRunners:
    def __init__(self, client: V2Client):
        self.client = client
        self._process_lock = threading.Lock()
        self._processes: set[ManagedProcess] = set()

    def cancel_all(self, reason: str = "dispatcher shutdown") -> None:
        with self._process_lock:
            processes = list(self._processes)
        for process in processes:
            process.cancel(reason)

    def run(
        self,
        action: DispatchAction,
        worker_snapshot: WorkerSnapshot,
        runtime: RuntimeConfig,
    ) -> None:
        if action.kind == "needs_replan":
            assert action.intent_id
            self.client.needs_replan(action.task_id, action.intent_id)
            return
        worker = self._worker(worker_snapshot)
        try:
            if action.kind == "bootstrap":
                assert action.intent_id
                self._run_intent(
                    task_id=action.task_id,
                    intent_id=action.intent_id,
                    phase="bootstrap",
                    worker=worker,
                    runtime=runtime,
                )
            elif action.kind == "explore":
                assert action.intent_id
                self._run_intent(
                    task_id=action.task_id,
                    intent_id=action.intent_id,
                    phase="explore",
                    worker=worker,
                    runtime=runtime,
                )
            elif action.kind == "reason":
                self._run_reason(action.task_id, worker, runtime)
        except Exception as exc:
            self._cooldown(worker.name, runtime, type(exc).__name__)
            raise

    def _run_intent(
        self,
        *,
        task_id: str,
        intent_id: str,
        phase: str,
        worker: WorkerConfig,
        runtime: RuntimeConfig,
    ) -> None:
        task = self.client.task(task_id)
        intent = next(item for item in self.client.intents(task_id) if item.id == intent_id)
        self.client.claim_intent(task_id, intent_id, worker.name)
        attempt_id = f"{phase}-{task_id}-{intent_id}-{uuid.uuid4().hex[:10]}"
        context = self.client.build_context(task_id, attempt_id)
        run_dir = self._run_dir(context, task_id, intent_id, attempt_id)
        env = self._phase_env(
            worker,
            task=task,
            phase=phase,
            intent_id=intent_id,
            context=context,
            run_dir=run_dir,
        )
        worker = worker.model_copy(update={"env": env})
        execute_timeout = (
            runtime.bootstrap_execute_seconds
            if phase == "bootstrap"
            else intent.execute_timeout_seconds or runtime.explore_execute_seconds
        )
        conclude_timeout = (
            runtime.bootstrap_conclude_seconds
            if phase == "bootstrap"
            else intent.conclude_timeout_seconds or runtime.explore_conclude_seconds
        )
        prompt = render_prompt(
            load_prompt("v2", f"{phase}.md"),
            {
                "task_id": task_id,
                "project_id": task.project_id,
                "intent_id": intent_id,
                "intent_description": intent.description,
                "origin": task.origin.original_input,
                "goal": task.goal,
                "manifest_path": context["manifest_path"],
                "run_dir": str(run_dir),
            },
        )
        lease = LeaseHeartbeat(
            lambda: self.client.heartbeat_intent(task_id, intent_id, worker.name),
            interval=runtime.heartbeat_interval_seconds,
        )
        lease.start()
        try:
            result, session, response = self._execute(
                worker, phase=phase, prompt=prompt, timeout=execute_timeout, lease=lease
            )
            if result.returncode != 0 and not result.timed_out:
                raise RunnerFailure(result.stderr or response or f"{phase} execute failed")
            if session:
                conclude_prompt = render_prompt(
                    load_prompt("v2", f"{phase}_conclude.md"),
                    {
                        "task_id": task_id,
                        "intent_id": intent_id,
                        "intent_description": intent.description,
                        "manifest_path": context["manifest_path"],
                        "run_dir": str(run_dir),
                    },
                )
                conclude_result, response = self._conclude(
                    worker,
                    phase=phase,
                    prompt=conclude_prompt,
                    session=session,
                    timeout=conclude_timeout,
                    lease=lease,
                )
                if conclude_result.returncode != 0:
                    raise RunnerFailure(conclude_result.stderr or response or "conclude failed")
            if lease.failure:
                raise RunnerFailure(f"lease heartbeat failed: {lease.failure}")
            description = response.strip() or f"{phase} completed; see registered artifacts"
            self.client.conclude_intent(task_id, intent_id, worker.name, description)
            self._audit(
                task,
                worker.name,
                f"{phase}.completed",
                f"{phase.title()} completed for {intent_id}",
                {"intent_id": intent_id, "attempt_id": attempt_id},
            )
        except Exception as exc:
            self._release_intent(task_id, intent, worker.name, phase, runtime, exc)
            raise
        finally:
            lease.stop()

    def _run_reason(self, task_id: str, worker: WorkerConfig, runtime: RuntimeConfig) -> None:
        task = self.client.claim_reason(task_id, worker.name)
        attempt_id = f"reason-{task_id}-{uuid.uuid4().hex[:10]}"
        context = self.client.build_context(task_id, attempt_id)
        workspace = self.client.open_workspace(
            task_id,
            worker=worker.name,
            context_revision=int(context["revision"]),
        )
        run_dir = self._run_dir(context, task_id, "reason", attempt_id)
        env = self._phase_env(
            worker,
            task=task,
            phase="reason",
            intent_id=None,
            context=context,
            run_dir=run_dir,
            workspace_id=workspace.id,
        )
        worker = worker.model_copy(update={"env": env})
        prompt = render_prompt(
            load_prompt("v2", "reason.md"),
            {
                "task_id": task_id,
                "project_id": task.project_id,
                "origin": task.origin.original_input,
                "goal": task.goal,
                "manifest_path": context["manifest_path"],
                "workspace_id": workspace.id,
            },
        )
        lease = LeaseHeartbeat(
            lambda: self.client.heartbeat_reason(task_id, worker.name),
            interval=runtime.heartbeat_interval_seconds,
        )
        lease.start()
        try:
            result, _session, response = self._execute(
                worker,
                phase="reason",
                prompt=prompt,
                timeout=runtime.reason_execute_seconds,
                lease=lease,
            )
            if result.returncode != 0:
                raise RunnerFailure(result.stderr or response or "reason failed")
            current_task = self.client.task(task_id)
            current_workspace = self.client.workspace(workspace.id)
            if current_task.status == "completed":
                if current_workspace.status == "open":
                    self.client.abandon_workspace(
                        workspace.id,
                        worker=worker.name,
                        failure_reason="Task completed because Goal was achieved",
                    )
                return
            if current_workspace.status != "committed":
                raise RunnerFailure("Reason exited without committing its Workspace")
            self.client.release_reason(task_id, worker.name)
            self._audit(
                task,
                worker.name,
                "reason.committed",
                f"Reason Workspace {workspace.id} committed",
                {"workspace_id": workspace.id, "attempt_id": attempt_id},
            )
        except Exception as exc:
            try:
                current = self.client.workspace(workspace.id)
                if current.status == "open":
                    self.client.abandon_workspace(
                        workspace.id,
                        worker=worker.name,
                        failure_reason=str(exc),
                    )
            except Exception:
                LOG.exception("failed to abandon reason workspace=%s", workspace.id)
            try:
                self.client.release_reason(task_id, worker.name)
            except Exception:
                LOG.exception("failed to release reason task=%s", task_id)
            raise
        finally:
            lease.stop()

    def _execute(
        self,
        worker: WorkerConfig,
        *,
        phase: str,
        prompt: str,
        timeout: int,
        lease: LeaseHeartbeat,
    ) -> tuple[ProcessResult, str | None, str]:
        driver = get_driver(worker.type)
        prepared_session = driver.prepare_session()
        command = driver.build_execute(worker, prompt, prepared_session, phase=phase)
        process = ManagedProcess(command.argv, worker.env)
        self._track(process)
        try:
            process.start()
            lease.attach(process)
            result = process.communicate(timeout=timeout)
        finally:
            lease.attach(None)
            self._untrack(process)
        session = driver.extract_session(command.session, result.stdout, result.stderr)
        response = driver.extract_response_text(result.stdout, result.stderr)
        return result, session, response

    def _conclude(
        self,
        worker: WorkerConfig,
        *,
        phase: str,
        prompt: str,
        session: str,
        timeout: int,
        lease: LeaseHeartbeat,
    ) -> tuple[ProcessResult, str]:
        driver = get_driver(worker.type)
        command = driver.build_conclude(worker, prompt, session, phase=phase)
        process = ManagedProcess(command, worker.env)
        self._track(process)
        try:
            process.start()
            lease.attach(process)
            result = process.communicate(timeout=timeout)
        finally:
            lease.attach(None)
            self._untrack(process)
        return result, driver.extract_response_text(result.stdout, result.stderr)

    def _release_intent(
        self,
        task_id,
        intent,
        worker,
        phase,
        runtime,
        exc,
    ) -> None:
        if phase == "bootstrap" and intent.retry_count + 1 >= 3:
            failure_kind, next_retry = "needs_hint", None
        else:
            failure_kind = type(exc).__name__.lower()
            delay = runtime.retry_base_seconds * min(2**intent.retry_count, 8)
            next_retry = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
        try:
            self.client.release_intent(
                task_id,
                intent.id,
                worker,
                failure_kind=failure_kind,
                next_retry_at=next_retry,
            )
        except Exception:
            LOG.exception("failed to release intent task=%s intent=%s", task_id, intent.id)

    def _cooldown(self, worker: str, runtime: RuntimeConfig, failure_kind: str) -> None:
        until = (
            datetime.now(timezone.utc)
            + timedelta(seconds=runtime.worker_failure_cooldown_seconds)
        ).isoformat()
        try:
            self.client.set_worker_cooldown(
                worker,
                failure_kind=failure_kind,
                cooldown_until=until,
            )
        except Exception:
            LOG.exception("failed to persist worker cooldown worker=%s", worker)

    def _audit(self, task, worker, event_type, message, payload) -> None:
        try:
            self.client.audit(
                {
                    "project_id": task.project_id,
                    "task_id": task.id,
                    "worker": worker,
                    "event_type": event_type,
                    "message": message,
                    "payload": payload,
                }
            )
        except Exception:
            LOG.exception("failed to write audit event type=%s", event_type)

    def _track(self, process: ManagedProcess) -> None:
        with self._process_lock:
            self._processes.add(process)

    def _untrack(self, process: ManagedProcess) -> None:
        with self._process_lock:
            self._processes.discard(process)

    def _phase_env(
        self,
        worker: WorkerConfig,
        *,
        task,
        phase: str,
        intent_id: str | None,
        context,
        run_dir: Path,
        workspace_id: str | None = None,
    ) -> dict[str, str]:
        env = dict(worker.env)
        env.update(
            {
                "AXIOM_V2": "1",
                "AXIOM_SERVER": self.client.base_url,
                "AXIOM_SERVICE_TOKEN": self.client.service_token,
                "AXIOM_PROJECT_ID": task.project_id,
                "AXIOM_TASK_ID": task.id,
                "AXIOM_WORKER": worker.name,
                "AXIOM_PHASE": phase,
                "AXIOM_CONTEXT_MANIFEST": context["manifest_path"],
                "AXIOM_RUN_DIR": str(run_dir),
                "AXIOM_GUARD_ENABLED": "1" if task.guard_enabled else "0",
                "AXIOM_TASK_SCOPE_ENABLED": "0",
            }
        )
        if intent_id:
            env["AXIOM_INTENT_ID"] = intent_id
        if workspace_id:
            env["AXIOM_REASON_WORKSPACE"] = workspace_id
        return env

    @staticmethod
    def _run_dir(context, task_id: str, subject: str, attempt_id: str) -> Path:
        data_dir = Path(context["manifest_path"]).resolve().parents[2]
        path = data_dir / "runs" / task_id / subject / attempt_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _worker(snapshot: WorkerSnapshot) -> WorkerConfig:
        config = snapshot.config
        raw_env = config.get("env", {}) if isinstance(config.get("env", {}), dict) else {}
        env = {str(key): str(value) for key, value in raw_env.items()}
        if isinstance(config.get("pi_tools"), list):
            import json

            env["AXIOM_PI_TOOLS_JSON"] = json.dumps(config["pi_tools"])
        return WorkerConfig(
            name=snapshot.name,
            type=snapshot.worker_type,
            task_types=list(snapshot.capabilities),
            max_running=snapshot.max_running,
            priority=max(snapshot.priority, 0),
            env=env,
            disabled=snapshot.disabled,
        )
