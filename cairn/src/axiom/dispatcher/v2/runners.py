from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from axiom.dispatcher.config import WorkerConfig
from axiom.dispatcher.prompting import load_prompt, render_prompt
from axiom.dispatcher.redaction import redact_sensitive_text
from axiom.dispatcher.runtime.process import ManagedProcess, ProcessResult
from axiom.dispatcher.runtime.rpc import ManagedRpcProcess
from axiom.dispatcher.runtime.activity import IntentActivityReporter
from axiom.dispatcher.token_usage import extract_token_usage, sum_token_usage
from axiom.dispatcher.v2.client import V2Client
from axiom.dispatcher.v2.explore_contract import (
    ExploreContractError,
    validate_explore_artifacts,
)
from axiom.dispatcher.v2.schemas import DispatchAction, WorkerSnapshot
from axiom.dispatcher.workers.registry import get_driver
from axiom.modules.settings.schemas import RuntimeConfig


LOG = logging.getLogger(__name__)
COMPACT_CONCLUDE_SESSION_BYTES = 128 * 1024
HEARTBEAT_GRACE_SECONDS = 30.0
FATAL_LEASE_STATUS_CODES = {401, 403, 404, 409}
BOOTSTRAP_ARTIFACT_BUNDLE = "bootstrap_artifacts.json"
CANONICAL_CLI_TOOLS = {
    "HTTPX": ("httpx",),
    "NMAP": ("nmap",),
    "SUBFINDER": ("subfinder",),
    "KATANA": ("katana",),
    "NUCLEI": ("nuclei",),
    "SQLMAP": ("sqlmap",),
    "DIRSEARCH": ("dirsearch", "dirsearch.py"),
    "URLFINDER": ("URLFinder", "urlfinder"),
}


REQUIRED_ARTIFACTS = {
    "bootstrap": (
        ("asset_inventory.md", "asset_inventory", "Broad attack-surface inventory"),
        ("initial_leads.md", "initial_leads", "Evidence-backed initial leads"),
        ("findings.md", "findings", "Verified Bootstrap findings"),
    ),
    "explore": (
        ("findings.md", "findings", "Verified Explore results"),
        ("leads.md", "leads", "Evidence-backed follow-up leads"),
    ),
}


class RunnerFailure(RuntimeError):
    pass


class LeaseHeartbeat:
    def __init__(self, callback: Callable[[], object], *, interval: int):
        self.callback = callback
        self.interval = interval
        self.stop_event = threading.Event()
        self.process: ManagedProcess | None = None
        self.failure: Exception | None = None
        self.started = False
        self.last_success_at = time.monotonic()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self.callback()
        self.last_success_at = time.monotonic()
        self.thread.start()
        self.started = True

    def attach(self, process: ManagedProcess | None) -> None:
        self.process = process

    def stop(self) -> None:
        self.stop_event.set()
        if self.started:
            self.thread.join(timeout=1)

    def _run(self) -> None:
        while not self.stop_event.wait(self.interval):
            try:
                self.callback()
                self.last_success_at = time.monotonic()
            except Exception as exc:
                status_code = getattr(exc, "status_code", None)
                elapsed = time.monotonic() - self.last_success_at
                if status_code in FATAL_LEASE_STATUS_CODES or elapsed >= HEARTBEAT_GRACE_SECONDS:
                    self.failure = exc
                    LOG.warning(
                        "intent lease heartbeat lost status=%s elapsed=%.1fs",
                        status_code,
                        elapsed,
                    )
                    if self.process is not None:
                        self.process.kill()
                    return
                LOG.warning(
                    "transient intent lease heartbeat failure status=%s elapsed=%.1fs; retrying",
                    status_code,
                    elapsed,
                )


class PhaseRunners:
    def __init__(self, client: V2Client):
        self.client = client
        self._process_lock = threading.Lock()
        self._processes: dict[object, str | None] = {}

    def cancel_all(self, reason: str = "dispatcher shutdown") -> None:
        with self._process_lock:
            processes = list(self._processes)
        for process in processes:
            process.cancel(reason)

    def cancel_task(self, task_id: str, reason: str = "task stopped") -> None:
        """Cancel every live phase process owned by one Task."""
        with self._process_lock:
            processes = [
                process for process, owner in self._processes.items() if owner == task_id
            ]
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
        driver = get_driver(worker.type)
        if getattr(driver, "supports_rpc", lambda: False)():
            self._run_rpc_intent(
                task_id=task_id,
                intent_id=intent_id,
                phase=phase,
                worker=worker,
                runtime=runtime,
            )
            return
        task = self.client.task(task_id)
        intent = next(item for item in self.client.intents(task_id) if item.id == intent_id)
        self.client.claim_intent(task_id, intent_id, worker.name)
        lease = LeaseHeartbeat(
            lambda: self.client.heartbeat_intent(task_id, intent_id, worker.name),
            interval=runtime.heartbeat_interval_seconds,
        )
        try:
            lease.start()
            # The lease heartbeat starts immediately after claim. Context and
            # Pi/environment initialization may involve filesystem and RPC
            # setup and can exceed the recovery threshold on a busy host.
            attempt_id = f"{phase}-{task_id}-{intent_id}-{uuid.uuid4().hex[:10]}"
            context = self.client.build_context(
                task_id,
                attempt_id,
                phase=phase,
                intent_id=intent_id,
            )
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
        except Exception as exc:
            lease.stop()
            if self._task_running(task_id):
                self._release_intent(task_id, intent, worker.name, phase, runtime, exc)
                raise
            return
        try:
            usage_parts = []
            registered_artifacts: dict[str, dict] = {}
            result, session, response = self._execute(
                worker,
                task_id=task_id,
                phase=phase,
                prompt=prompt,
                timeout=execute_timeout,
                lease=lease,
            )
            if result.cancelled or not self._task_running(task_id):
                LOG.info(
                    "phase cancelled after execute task=%s intent=%s phase=%s reason=%s",
                    task_id,
                    intent_id,
                    phase,
                    result.cancel_reason,
                )
                return
            self._redact_session_material(run_dir)
            usage_parts.append(extract_token_usage(result.stdout))
            execute_budget_exhausted = result.timed_out
            if result.returncode != 0 and not execute_budget_exhausted:
                raise RunnerFailure(result.stderr or response or f"{phase} execute failed")
            if execute_budget_exhausted and not session:
                raise RunnerFailure(
                    f"{phase} execute budget exhausted and its PI session could not be recovered"
                )
            if execute_budget_exhausted:
                self._audit(
                    task,
                    worker.name,
                    f"{phase}.execute_budget_exhausted",
                    f"{phase.title()} exploration budget exhausted; forcing conclusion for {intent_id}",
                    {
                        "intent_id": intent_id,
                        "attempt_id": attempt_id,
                        "execute_budget_exhausted": True,
                        "execute_budget_seconds": execute_timeout,
                    },
                )

            summary: str | None = None
            summary_source = "bootstrap_artifact_summary" if phase == "bootstrap" else ""
            if phase == "explore" and not execute_budget_exhausted:
                try:
                    summary = self._validated_explore_summary(response)
                    summary_source = "explore_execute_summary"
                except RunnerFailure as exc:
                    self._audit(
                        task,
                        worker.name,
                        "explore.summary_invalid",
                        f"Explore execute summary did not satisfy the structured conclusion contract for {intent_id}",
                        {"intent_id": intent_id, "stage": "execute", "error": str(exc)},
                    )

            # The prompt asks the model to register its files through
            # axiom-tool. In practice models sometimes write the required
            # files but omit the registration command. Reconcile files from
            # the isolated run directory before the lifecycle conclude call.
            # Reconcile files before deciding whether a separate conclusion turn
            # is needed.  A complete artifact set is not sufficient by itself:
            # Explore must also return a validated structured summary.
            missing = self._reconcile_required_artifacts(
                task_id=task_id,
                intent_id=intent_id,
                phase=phase,
                worker=worker.name,
                run_dir=run_dir,
                registered_artifacts=registered_artifacts,
            )
            if phase == "explore" and summary is None and not missing:
                summary = self._artifact_fallback_summary(
                    task=task,
                    task_id=task_id,
                    intent_id=intent_id,
                    worker=worker.name,
                    run_dir=run_dir,
                    registered_artifacts=registered_artifacts,
                    reason="worker returned no valid Explore summary",
                )
                summary_source = "explore_artifact_summary"
            conclude_result: ProcessResult | None = None
            compact_conclude = False
            evidence_path: Path | None = None
            needs_conclude = bool(missing) or (phase == "explore" and summary is None)
            if needs_conclude and not session:
                if phase == "explore" and summary is None:
                    raise RunnerFailure(
                        "explore summary contract invalid and its worker session could not be recovered"
                    )
                raise RunnerFailure(
                    f"{phase} required artifacts missing and its worker session could not be recovered"
                )
            if session and needs_conclude:
                driver = get_driver(worker.type)
                session_size_method = getattr(driver, "session_size_bytes", None)
                session_size = (
                    session_size_method(worker, session)
                    if session_size_method is not None
                    else None
                )
                supports_compact = bool(
                    getattr(driver, "supports_compact_conclude", lambda: False)()
                )
                compact_conclude = bool(
                    phase == "bootstrap"
                    and supports_compact
                    and (
                        execute_budget_exhausted
                        or (session_size is not None and session_size >= COMPACT_CONCLUDE_SESSION_BYTES)
                    )
                )
                if compact_conclude:
                    export_evidence = getattr(driver, "export_compact_evidence", None)
                    evidence_path = (
                        export_evidence(worker, session, run_dir / "execute_evidence.md")
                        if export_evidence is not None
                        else None
                    )
                    # A fresh session without an evidence digest would lose
                    # the execute turn's findings, so fall back to the old
                    # resumable path if export is unavailable.
                    compact_conclude = evidence_path is not None
                conclude_prompt = render_prompt(
                    load_prompt("v2", f"{phase}_conclude.md"),
                    {
                        "task_id": task_id,
                        "intent_id": intent_id,
                        "intent_description": intent.description,
                        "manifest_path": context["manifest_path"],
                        "run_dir": str(run_dir),
                        "evidence_path": str(evidence_path or ""),
                        "missing_artifacts": ", ".join(missing),
                    },
                )
                conclude_kwargs = {
                    "phase": phase,
                    "prompt": conclude_prompt,
                    "session": session,
                    "timeout": conclude_timeout,
                    "lease": lease,
                }
                if compact_conclude:
                    conclude_kwargs["compact"] = True
                conclude_result, response = self._conclude(
                    worker, task_id=task_id, **conclude_kwargs
                )
                if conclude_result.cancelled or not self._task_running(task_id):
                    LOG.info(
                        "phase cancelled after conclude task=%s intent=%s phase=%s reason=%s",
                        task_id,
                        intent_id,
                        phase,
                        conclude_result.cancel_reason,
                    )
                    return
                usage_parts.append(extract_token_usage(conclude_result.stdout))
                if conclude_result.timed_out:
                    # A timeout after all required files were durably written
                    # is a late response timeout, not a failed phase. Verify
                    # the contract before deciding the lifecycle outcome.
                    missing_after_timeout = self._reconcile_required_artifacts(
                        task_id=task_id,
                        intent_id=intent_id,
                        phase=phase,
                        worker=worker.name,
                        run_dir=run_dir,
                        registered_artifacts=registered_artifacts,
                    )
                    if phase == "bootstrap" and missing_after_timeout:
                        self._bootstrap_artifact_fallback(
                            task=task,
                            task_id=task_id,
                            intent_id=intent_id,
                            worker=worker.name,
                            run_dir=run_dir,
                            missing=missing_after_timeout,
                            reason="conclude timeout after execute evidence was durable",
                        )
                        missing_after_timeout = self._reconcile_required_artifacts(
                            task_id=task_id,
                            intent_id=intent_id,
                            phase=phase,
                            worker=worker.name,
                            run_dir=run_dir,
                            registered_artifacts=registered_artifacts,
                        )
                    if missing_after_timeout:
                        raise RunnerFailure(
                            f"{phase} conclude timeout: forced-conclusion budget exhausted "
                            f"after {conclude_timeout} seconds; missing: "
                            f"{', '.join(missing_after_timeout)}"
                        )
                    if phase == "explore":
                        summary = self._artifact_fallback_summary(
                            task=task,
                            task_id=task_id,
                            intent_id=intent_id,
                            worker=worker.name,
                            run_dir=run_dir,
                            registered_artifacts=registered_artifacts,
                            reason="conclude timed out after required artifacts were durable",
                        )
                        summary_source = "explore_artifact_summary"
                    response = response or f"{phase} completed; artifacts were written before conclude timeout"
                elif conclude_result.returncode != 0:
                    if phase == "bootstrap" and self._bootstrap_evidence_files(run_dir):
                        self._audit(
                            task,
                            worker.name,
                            "bootstrap.conclude_process_fallback",
                            f"Bootstrap conclude process failed after durable evidence; using conservative fallback for {intent_id}",
                            {
                                "intent_id": intent_id,
                                "returncode": conclude_result.returncode,
                                "error": redact_sensitive_text(
                                    conclude_result.stderr or response or "conclude failed"
                                )[:500],
                            },
                        )
                    else:
                        raise RunnerFailure(conclude_result.stderr or response or "conclude failed")
                if phase == "explore" and summary is None:
                    try:
                        summary = self._validated_explore_summary(response)
                        summary_source = "explore_conclude_summary"
                    except RunnerFailure:
                        if missing:
                            raise
                        summary = self._artifact_fallback_summary(
                            task=task,
                            task_id=task_id,
                            intent_id=intent_id,
                            worker=worker.name,
                            run_dir=run_dir,
                            registered_artifacts=registered_artifacts,
                            reason="conclude returned no valid Explore summary",
                        )
                        summary_source = "explore_artifact_summary"
            missing = self._reconcile_required_artifacts(
                task_id=task_id,
                intent_id=intent_id,
                phase=phase,
                worker=worker.name,
                run_dir=run_dir,
                registered_artifacts=registered_artifacts,
            )
            if phase == "bootstrap" and missing:
                self._bootstrap_artifact_fallback(
                    task=task,
                    task_id=task_id,
                    intent_id=intent_id,
                    worker=worker.name,
                    run_dir=run_dir,
                    missing=missing,
                    reason="close-out budget exhausted with durable execute evidence",
                )
                missing = self._reconcile_required_artifacts(
                    task_id=task_id,
                    intent_id=intent_id,
                    phase=phase,
                    worker=worker.name,
                    run_dir=run_dir,
                    registered_artifacts=registered_artifacts,
                )
            if missing:
                raise RunnerFailure(
                    f"{phase} required artifacts missing after conclusion: {', '.join(missing)}"
                )
            if not self._task_running(task_id):
                return
            if lease.failure:
                raise RunnerFailure(f"lease heartbeat failed: {lease.failure}")
            description = self._fact_description(
                phase=phase,
                run_dir=run_dir,
                registered_artifacts=registered_artifacts,
                summary=summary,
                fallback=response,
            )
            self.client.conclude_intent(
                task_id,
                intent_id,
                worker.name,
                description,
                summary_source=summary_source,
            )
            payload = {
                "intent_id": intent_id,
                "attempt_id": attempt_id,
                "execute_budget_exhausted": execute_budget_exhausted,
            }
            if conclude_result is not None:
                payload["compact_conclude"] = compact_conclude
                payload["conclude_timed_out_after_artifacts"] = conclude_result.timed_out
            token_usage = sum_token_usage(usage_parts)
            if token_usage is not None:
                payload["token_usage"] = token_usage
            self._audit(
                task,
                worker.name,
                f"{phase}.completed",
                f"{phase.title()} completed for {intent_id}",
                payload,
            )
        except Exception as exc:
            if not self._task_running(task_id):
                LOG.info(
                    "phase stopped after task became inactive task=%s intent=%s phase=%s worker=%s",
                    task_id,
                    intent_id,
                    phase,
                    worker.name,
                )
                return
            self._release_intent(task_id, intent, worker.name, phase, runtime, exc)
            raise
        finally:
            self._redact_session_material(run_dir)
            self._cleanup_private_credential_files(run_dir)
            lease.stop()

    def _run_rpc_intent(
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
        lease = LeaseHeartbeat(
            lambda: self.client.heartbeat_intent(task_id, intent_id, worker.name),
            interval=runtime.heartbeat_interval_seconds,
        )
        try:
            lease.start()
            # Keep the lease alive across context, session and RPC startup.
            attempt_id = f"{phase}-{task_id}-{intent_id}-{uuid.uuid4().hex[:10]}"
            context = self.client.build_context(
                task_id,
                attempt_id,
                phase=phase,
                intent_id=intent_id,
            )
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
            driver = get_driver(worker.type)
            prepared_session = driver.prepare_session()
            rpc = ManagedRpcProcess(
                driver.build_rpc(worker, prepared_session, phase=phase),
                worker.env,
            )
            activity = None
            if hasattr(self.client, "report_intent_activity"):
                activity = IntentActivityReporter(
                    self.client,
                    task_id=task_id,
                    intent_id=intent_id,
                    worker=worker.name,
                    targets=intent.targets or [task.origin.host],
                )
                set_event_callback = getattr(rpc, "set_event_callback", None)
                if set_event_callback is not None:
                    set_event_callback(activity.handle_event)
            self._track(rpc, task_id)
        except Exception as exc:
            lease.stop()
            if self._task_running(task_id):
                self._release_intent(task_id, intent, worker.name, phase, runtime, exc)
                raise
            return
        usage_parts = []
        registered_artifacts: dict[str, dict] = {}
        rpc_closed = False
        deadline_stage = {"value": "execute"}
        execute_stop_grace = min(30, max(5, conclude_timeout / 2))

        def record_deadline(event: str, details: dict) -> None:
            stage = deadline_stage["value"]
            payload = {
                "intent_id": intent_id,
                "attempt_id": attempt_id,
                "stage": stage,
                **details,
            }
            if event == "soft_deadline":
                if activity is not None:
                    activity.report(
                        "concluding",
                        "探索预算已到，正在停止当前执行回合",
                        force=True,
                    )
                self._audit(
                    task,
                    worker.name,
                    f"{phase}.soft_deadline",
                    f"{phase.title()} execute budget reached; stopping the live RPC turn",
                    payload,
                )
            elif event == "soft_abort":
                self._audit(
                    task,
                    worker.name,
                    f"{phase}.execute_abort",
                    f"{phase.title()} execute turn was aborted after the stop grace elapsed",
                    payload,
                )
            elif event == "hard_deadline":
                event_type = (
                    f"{phase}.hard_deadline"
                    if stage == "execute"
                    else f"{phase}.conclude_hard_deadline"
                )
                self._audit(
                    task,
                    worker.name,
                    event_type,
                    f"{phase.title()} {stage} hard deadline reached",
                    payload,
                )

        set_deadline_callback = getattr(rpc, "set_deadline_callback", None)
        if set_deadline_callback is not None:
            set_deadline_callback(record_deadline)
        try:
            rpc.start()
            lease.attach(rpc)
            stop_prompt = (
                "# Stop Execute\n\n"
                "The exploration budget is exhausted. Stop the current investigation turn "
                "immediately. Do not start another tool, do not write phase artifacts in this "
                "turn, and return a short acknowledgement. AXIOM will start a separately timed "
                "Conclude turn next."
            )
            result = rpc.prompt(
                prompt,
                soft_timeout=execute_timeout,
                hard_timeout=execute_timeout + execute_stop_grace + 10,
                steering_message=stop_prompt,
                abort_after_steer=execute_stop_grace,
            )
            if result.cancelled or not self._task_running(task_id):
                LOG.info(
                    "RPC phase cancelled after execute task=%s intent=%s phase=%s reason=%s",
                    task_id,
                    intent_id,
                    phase,
                    result.cancel_reason,
                )
                return
            usage_parts.append(extract_token_usage(result.stdout))
            # The installed Pi RPC may emit a different real session id from
            # the one preallocated for --session-id. Resolve the session from
            # the event stream/attempt directory before evidence export and
            # close-out, otherwise conclude attempts resume a non-existent id.
            recover_session = getattr(driver, "recover_session", None)
            if recover_session is not None:
                actual_session = recover_session(
                    worker,
                    prepared_session,
                    result.stdout,
                    result.stderr,
                )
                if actual_session:
                    if actual_session != prepared_session:
                        self._audit(
                            task,
                            worker.name,
                            f"{phase}.session_recovered",
                            f"{phase.title()} resolved the actual Pi session before close-out",
                            {
                                "intent_id": intent_id,
                                "attempt_id": attempt_id,
                                "prepared_session": bool(prepared_session),
                                "session_recovered": True,
                            },
                        )
                    prepared_session = actual_session
            self._redact_session_material(run_dir)
            response = driver.extract_response_text(result.stdout, result.stderr)
            if result.returncode != 0 and not result.timed_out:
                raise RunnerFailure(result.stderr or response or f"{phase} execute failed")
            summary: str | None = None
            summary_source = ""
            if phase == "explore" and not result.timed_out:
                try:
                    summary = self._validated_explore_summary(response)
                    summary_source = "explore_execute_summary"
                except RunnerFailure as exc:
                    self._audit(
                        task,
                        worker.name,
                        "explore.summary_invalid",
                        f"Explore execute summary did not satisfy the structured conclusion contract for {intent_id}",
                        {"intent_id": intent_id, "stage": "execute", "error": str(exc)},
                    )
            self._materialize_artifact_bundle(run_dir=run_dir, phase=phase)
            missing = self._reconcile_required_artifacts(
                task_id=task_id,
                intent_id=intent_id,
                phase=phase,
                worker=worker.name,
                run_dir=run_dir,
                registered_artifacts=registered_artifacts,
            )
            closure_deadline = time.monotonic() + conclude_timeout
            if (
                phase == "explore"
                and summary is None
                and not missing
                and not result.timed_out
            ):
                repaired, repaired_response, repair_result = (
                    self._repair_explore_summary_in_rpc(
                        rpc=rpc,
                        driver=driver,
                        task=task,
                        worker=worker,
                        task_id=task_id,
                        intent_id=intent_id,
                        run_dir=run_dir,
                        deadline=closure_deadline,
                        usage_parts=usage_parts,
                    )
                )
                if (
                    getattr(repair_result, "cancelled", False)
                    or not self._task_running(task_id)
                ):
                    return
                if repaired is not None:
                    summary = repaired
                    response = repaired_response
                    # The retry is still an Execute-session summary.  Keep the
                    # durable provenance within the backend's public contract;
                    # the fact that it took a repair turn belongs in audit.
                    summary_source = "explore_execute_summary"
                elif not missing:
                    summary = self._artifact_fallback_summary(
                        task=task,
                        task_id=task_id,
                        intent_id=intent_id,
                        worker=worker.name,
                        run_dir=run_dir,
                        registered_artifacts=registered_artifacts,
                        reason="same-session JSON repair exhausted",
                    )
                    summary_source = "explore_artifact_summary"
            conclude_result: ProcessResult | None = None
            compact_conclude = False
            needs_conclude = bool(missing) or (phase == "explore" and summary is None)
            if needs_conclude:
                evidence_path: Path | None = None
                export_evidence = getattr(driver, "export_compact_evidence", None)
                if export_evidence is not None and prepared_session:
                    evidence_path = export_evidence(
                        worker,
                        prepared_session,
                        run_dir / "execute_evidence.md",
                    )
                if activity is not None:
                    activity.report("concluding", "正在独立收口并生成结构化摘要", force=True)
                if not self._task_running(task_id):
                    return
                # End the execute RPC before starting the close-out process.
                # The conclude timeout must begin here, not at execute start.
                lease.attach(None)
                rpc.stop()
                self._untrack(rpc)
                rpc_closed = True
                deadline_stage["value"] = "conclude"
                self._audit(
                    task,
                    worker.name,
                    f"{phase}.conclude_started",
                    f"{phase.title()} conclude started with an independent budget",
                    {
                        "intent_id": intent_id,
                        "attempt_id": attempt_id,
                        "conclude_timeout_seconds": conclude_timeout,
                        "missing_artifacts": missing,
                        "compact_evidence": bool(evidence_path),
                    },
                )
                compact_conclude = bool(
                    getattr(driver, "supports_compact_conclude", lambda: False)()
                )
                # A compact session is safe when Explore artifacts already
                # exist, or when a bounded evidence digest was exported for a
                # timeout/missing-artifact close-out. Otherwise resume the
                # original session so its evidence is not discarded.
                compact_conclude = compact_conclude and (
                    not missing or evidence_path is not None
                )
                conclude_result, conclude_response, missing, summary, closeout_source = (
                    self._run_closeout_attempts(
                        task=task,
                        worker=worker,
                        task_id=task_id,
                        intent=intent,
                        phase=phase,
                        context=context,
                        run_dir=run_dir,
                        session=prepared_session or "",
                        evidence_path=evidence_path,
                        compact=compact_conclude,
                        deadline=closure_deadline,
                        lease=lease,
                        registered_artifacts=registered_artifacts,
                        usage_parts=usage_parts,
                        timeout_callback=lambda timeout: record_deadline(
                            "hard_deadline",
                            {
                                "elapsed_seconds": timeout,
                                "hard_timeout_seconds": timeout,
                            },
                        ),
                    )
                )
                response = conclude_response or response
                if closeout_source:
                    summary_source = closeout_source
                if (
                    conclude_result is not None
                    and conclude_result.cancelled
                ) or not self._task_running(task_id):
                    return
            if phase == "explore" and summary is None and not missing:
                summary = self._artifact_fallback_summary(
                    task=task,
                    task_id=task_id,
                    intent_id=intent_id,
                    worker=worker.name,
                    run_dir=run_dir,
                    registered_artifacts=registered_artifacts,
                    reason="bounded close-out attempts exhausted",
                )
                summary_source = "explore_artifact_summary"
            if missing:
                if phase == "bootstrap":
                    self._bootstrap_artifact_fallback(
                        task=task,
                        task_id=task_id,
                        intent_id=intent_id,
                        worker=worker.name,
                        run_dir=run_dir,
                        missing=missing,
                        reason="RPC close-out attempts exhausted with durable execute evidence",
                    )
                    missing = self._reconcile_required_artifacts(
                        task_id=task_id,
                        intent_id=intent_id,
                        phase=phase,
                        worker=worker.name,
                        run_dir=run_dir,
                        registered_artifacts=registered_artifacts,
                    )
            if missing:
                raise RunnerFailure(
                    f"{phase} required artifacts missing after RPC conclusion: {', '.join(missing)}"
                )
            if lease.failure:
                raise RunnerFailure(f"lease heartbeat failed: {lease.failure}")
            description = self._fact_description(
                phase=phase,
                run_dir=run_dir,
                registered_artifacts=registered_artifacts,
                summary=summary,
                fallback=response,
            )
            if activity is not None:
                activity.report("submitting", "正在提交测试结果", force=True)
            if not self._task_running(task_id):
                return
            self.client.conclude_intent(
                task_id,
                intent_id,
                worker.name,
                description,
                summary_source=summary_source or "bootstrap_artifact_summary",
            )
            payload = {
                "intent_id": intent_id,
                "attempt_id": attempt_id,
                "rpc": True,
                "soft_steered": result.soft_steered,
                "soft_aborted": result.soft_aborted,
                "hard_timed_out": result.timed_out
                or bool(conclude_result and conclude_result.timed_out),
                "compact_conclude": compact_conclude,
            }
            token_usage = sum_token_usage(usage_parts)
            if token_usage is not None:
                payload["token_usage"] = token_usage
            self._audit(
                task,
                worker.name,
                f"{phase}.completed",
                f"{phase.title()} completed for {intent_id}",
                payload,
            )
        except Exception as exc:
            if not self._task_running(task_id):
                return
            self._release_intent(task_id, intent, worker.name, phase, runtime, exc)
            raise
        finally:
            self._redact_session_material(run_dir)
            self._cleanup_private_credential_files(run_dir)
            lease.attach(None)
            lease.stop()
            if not rpc_closed:
                rpc.stop()
                self._untrack(rpc)
            if activity is not None:
                activity.close()

    @staticmethod
    def _materialize_artifact_bundle(*, run_dir: Path, phase: str) -> None:
        """Expand the single close-out submission into phase artifacts.

        Bootstrap conclude writes one JSON bundle.  The runner owns the final
        filesystem writes so three slow model tool calls cannot consume the
        close-out budget one after another.  Existing legacy per-file output
        remains supported for old sessions and non-bundle workers.
        """
        if phase != "bootstrap":
            return
        bundle_path = run_dir / BOOTSTRAP_ARTIFACT_BUNDLE
        if not bundle_path.is_file() or bundle_path.stat().st_size <= 0:
            return
        try:
            payload = json.loads(bundle_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            LOG.warning("invalid bootstrap artifact bundle path=%s", bundle_path)
            return
        if not isinstance(payload, dict):
            return
        fields = {
            "asset_inventory": "asset_inventory.md",
            "asset_inventory.md": "asset_inventory.md",
            "initial_leads": "initial_leads.md",
            "initial_leads.md": "initial_leads.md",
            "findings": "findings.md",
            "findings.md": "findings.md",
        }
        for key, filename in fields.items():
            content = payload.get(key)
            if not isinstance(content, str) or not content.strip():
                continue
            destination = run_dir / filename
            temporary = destination.with_name(f".{destination.name}.tmp")
            try:
                temporary.write_text(content.strip() + "\n", encoding="utf-8")
                temporary.replace(destination)
            except OSError:
                LOG.exception("failed to materialize bootstrap artifact path=%s", destination)

    @staticmethod
    def _cleanup_private_credential_files(run_dir: Path) -> None:
        """Delete transient credential material after every phase attempt."""

        try:
            candidates = list(run_dir.rglob("credential-*.json"))
        except OSError:
            return
        for path in candidates:
            try:
                if path.is_file():
                    path.unlink()
            except OSError:
                LOG.exception("failed to remove private credential file path=%s", path)

    @staticmethod
    def _redact_session_material(run_dir: Path) -> None:
        """Scrub transcript material before it can be reused as durable context."""

        candidates: list[Path] = []
        session_root = run_dir / ".pi-session"
        try:
            if session_root.is_dir():
                candidates.extend(session_root.rglob("*.jsonl"))
        except OSError:
            pass
        evidence_path = run_dir / "execute_evidence.md"
        if evidence_path.is_file():
            candidates.append(evidence_path)
        for path in candidates:
            try:
                original = path.read_text(encoding="utf-8", errors="replace")
                redacted = redact_sensitive_text(original)
                if redacted == original:
                    continue
                temporary = path.with_name(f".{path.name}.redacted")
                temporary.write_text(redacted, encoding="utf-8")
                temporary.replace(path)
            except OSError:
                LOG.exception("failed to redact session material path=%s", path)

    def _bootstrap_artifact_fallback(
        self,
        *,
        task,
        task_id: str,
        intent_id: str,
        worker: str,
        run_dir: Path,
        missing: list[str],
        reason: str,
    ) -> bool:
        """Close Bootstrap conservatively when the model misses its bundle.

        Bootstrap is an attack-surface discovery phase.  If the worker has
        already left durable scan output but spends its final close-out budget
        on context replay or malformed JSON, releasing the Intent loses useful
        work and needlessly lowers the natural close rate.  This fallback only
        indexes files that actually exist in the isolated run directory; it
        never invents a vulnerability or creates a vulnerability record.
        """
        evidence = self._bootstrap_evidence_files(run_dir)
        if not evidence:
            return False

        evidence_lines: list[str] = []
        preview_lines: list[str] = []
        for path in evidence:
            try:
                size = path.stat().st_size
            except OSError:
                continue
            relative = path.relative_to(run_dir).as_posix()
            evidence_lines.append(f"- `{relative}`（{size} 字节）")
            if path.suffix.lower() in {".txt", ".json", ".jsonl", ".md", ".csv"}:
                preview = self._artifact_preview(path, limit=700)
                if preview:
                    preview_lines.append(
                        f"`{relative}`：{redact_sensitive_text(preview)}"
                    )
        if not evidence_lines:
            return False

        asset_inventory = "\n".join(
            (
                "# 攻击面覆盖",
                "",
                "本轮 Bootstrap 未能完成模型最终 bundle，但以下证据文件已实际落盘。"
                "本摘要只做证据索引，不新增模型未证明的结论。",
                "",
                *evidence_lines,
                "",
                "证据摘要：",
                *preview_lines[:8],
            )
        )
        initial_leads = "\n".join(
            (
                "# 重要线索",
                "",
                "以下内容仅作为 Reason 的后续读取入口，当前均未验证为漏洞：",
                "",
                *evidence_lines,
                "",
                "请基于原始证据文件继续确认具体接口、参数、认证边界和实际影响；"
                "不得仅凭状态码、版本信息、响应头或路径名称升级为漏洞。",
            )
        )
        findings = "\n".join(
            (
                "# 确认结果",
                "",
                "本轮未形成满足漏洞标准的确认结果。",
                "配置观察、HTTP 200、版本信息、安全头缺失和理论风险不计入确认漏洞。",
                "已落盘证据请由后续 Explore 在有直接请求/响应证据时重新验证。",
            )
        )
        contents = {
            "asset_inventory.md": asset_inventory,
            "initial_leads.md": initial_leads,
            "findings.md": findings,
        }
        written: list[str] = []
        for filename in missing:
            content = contents.get(filename)
            if not content:
                continue
            path = run_dir / filename
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_name(f".{path.name}.fallback.tmp")
                temporary.write_text(content.strip() + "\n", encoding="utf-8")
                temporary.replace(path)
                written.append(filename)
            except OSError:
                LOG.exception("failed to write Bootstrap fallback artifact path=%s", path)
        if not written:
            return False
        self._audit(
            task,
            worker,
            "bootstrap.artifact_fallback",
            f"Bootstrap used a conservative artifact fallback for {intent_id}",
            {
                "intent_id": intent_id,
                "reason": reason,
                "written": written,
                "evidence_files": [path.relative_to(run_dir).as_posix() for path in evidence],
                "confirmed_findings_created": False,
            },
        )
        return True

    @staticmethod
    def _bootstrap_evidence_files(run_dir: Path) -> list[Path]:
        excluded = {
            "asset_inventory.md",
            "initial_leads.md",
            "findings.md",
            BOOTSTRAP_ARTIFACT_BUNDLE,
        }
        try:
            paths = sorted(run_dir.rglob("*"))
        except OSError:
            return []
        result: list[Path] = []
        for path in paths:
            if not path.is_file() or path.name in excluded:
                continue
            if path.name.startswith("credential-"):
                continue
            try:
                relative = path.relative_to(run_dir)
                if relative.parts and relative.parts[0].startswith("."):
                    continue
                if path.stat().st_size <= 0:
                    continue
            except OSError:
                continue
            result.append(path)
        return result

    def _reconcile_required_artifacts(
        self,
        *,
        task_id: str,
        intent_id: str,
        phase: str,
        worker: str,
        run_dir: Path,
        registered_artifacts: dict[str, dict] | None = None,
    ) -> list[str]:
        if phase == "bootstrap":
            self._reconcile_bootstrap_findings_contract(
                task_id=task_id,
                intent_id=intent_id,
                run_dir=run_dir,
            )
        elif phase == "explore":
            try:
                validate_explore_artifacts(run_dir)
            except ExploreContractError as exc:
                LOG.info(
                    "Explore artifact contract rejected task=%s intent=%s: %s",
                    task_id,
                    intent_id,
                    exc,
                )
                return [str(exc)]
        specs = REQUIRED_ARTIFACTS.get(phase, ())
        registered: list[str] = []
        missing: list[str] = []
        for filename, artifact_type, summary in specs:
            path = run_dir / filename
            if not path.is_file() or path.stat().st_size <= 0:
                missing.append(filename)
                continue
            self._redact_artifact(path)
            artifact = self.client.register_artifact(
                task_id,
                intent_id=intent_id,
                artifact_type=artifact_type,
                path=str(path),
                summary=summary,
                actor=f"{worker}.auto_finalize",
            )
            if registered_artifacts is not None:
                registered_artifacts[artifact_type] = artifact
            registered.append(filename)
        if registered or missing:
            LOG.info(
                "reconciled phase artifacts task=%s intent=%s phase=%s registered=%s missing=%s",
                task_id,
                intent_id,
                phase,
                registered,
                missing,
            )
        return missing

    def _reconcile_bootstrap_findings_contract(
        self,
        *,
        task_id: str,
        intent_id: str,
        run_dir: Path,
    ) -> None:
        """Make the structured vulnerability store the source of truth.

        A Bootstrap model can still write an over-confident claim into
        findings.md even when it never completed the vulnerability report.
        Such prose must not become a confirmed graph result. Production clients
        expose list_vulnerabilities; lightweight test/legacy clients without
        that method retain their previous behavior.
        """

        list_vulnerabilities = getattr(self.client, "list_vulnerabilities", None)
        findings_path = run_dir / "findings.md"
        if list_vulnerabilities is None or not findings_path.is_file():
            return
        vulnerabilities = list_vulnerabilities(task_id)
        confirmed = [
            item
            for item in vulnerabilities
            if not item.get("archived") and item.get("intent_id") == intent_id
        ]
        try:
            original = findings_path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return

        if confirmed:
            normalized = self._render_bootstrap_findings(confirmed)
        else:
            normalized = (
                "# 确认结果\n\n"
                "本轮 Bootstrap 未形成结构化确认漏洞记录。未完成漏洞上报契约的观察"
                "不得作为确认漏洞；相关方向应保留在 initial_leads.md 中继续验证。"
            )
            if original and original != normalized:
                LOG.warning(
                    "discarding Bootstrap findings without structured report task=%s intent=%s",
                    task_id,
                    intent_id,
                )

        normalized = redact_sensitive_text(normalized).strip() + "\n"
        if original + "\n" == normalized:
            return
        temporary = findings_path.with_name(f".{findings_path.name}.contract")
        temporary.write_text(normalized, encoding="utf-8")
        temporary.replace(findings_path)

    @staticmethod
    def _render_bootstrap_findings(vulnerabilities: list[dict]) -> str:
        lines = [
            "# 确认结果",
            "",
            "本文件仅列出已经通过 AXIOM 结构化漏洞上报形成的确认记录。",
        ]
        for item in vulnerabilities:
            vuln_id = str(item.get("id") or "未编号")
            title = str(item.get("title") or "未命名漏洞").strip()
            lines.extend(
                [
                    "",
                    f"## {vuln_id} · {title}",
                    "",
                    f"- 严重性：{item.get('severity') or '未标注'}",
                    f"- 类型：{item.get('type') or '未标注'}",
                    f"- 受影响目标：{item.get('affected_target') or '未标注'}",
                    f"- 状态：{item.get('status') or '未标注'}",
                    "",
                    "### 描述",
                    "",
                    str(item.get("description") or "未提供。").strip(),
                    "",
                    "### 前置条件",
                    "",
                    str(item.get("prerequisites") or "无特殊前置条件。").strip(),
                    "",
                    "### 实际影响",
                    "",
                    str(item.get("impact") or "未提供。").strip(),
                    "",
                    "### 复现步骤",
                    "",
                ]
            )
            steps = item.get("reproduction_steps")
            if isinstance(steps, list) and steps:
                lines.extend(
                    f"{index}. {str(step).strip()}"
                    for index, step in enumerate(steps, start=1)
                )
            else:
                lines.append("1. 未提供结构化复现步骤。")
            lines.extend(["", "### 证据", ""])
            evidence = item.get("evidence")
            if isinstance(evidence, list) and evidence:
                lines.extend(f"- {str(entry).strip()}" for entry in evidence)
            else:
                lines.append("- 未提供结构化证据。")
            curl_command = str(item.get("curl_command") or "").strip()
            if curl_command:
                lines.extend(["", "### 复现命令", "", "~~~bash", curl_command, "~~~"])
        return "\n".join(lines)

    @staticmethod
    def _redact_artifact(path: Path) -> None:
        """Scrub secrets before a worker file becomes durable context."""

        try:
            original = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        redacted = redact_sensitive_text(original)
        if redacted == original:
            return
        temporary = path.with_name(f".{path.name}.redacted")
        temporary.write_text(redacted, encoding="utf-8")
        temporary.replace(path)

    def _fact_description(
        self,
        *,
        phase: str,
        run_dir: Path,
        registered_artifacts: dict[str, dict],
    ) -> str:
        if phase != "bootstrap":
            try:
                return validate_explore_artifacts(run_dir).fact
            except ExploreContractError as exc:
                raise RunnerFailure(f"invalid Explore artifacts: {exc}") from exc

        labels = {
            "asset_inventory": "攻击面覆盖",
            "initial_leads": "重要线索",
            "findings": "确认结果",
        }
        lines = ["Bootstrap 已根据注册产物完成收口。"]
        index: list[str] = []
        for filename, artifact_type, _summary in REQUIRED_ARTIFACTS["bootstrap"]:
            artifact = registered_artifacts.get(artifact_type, {})
            artifact_id = str(artifact.get("id") or artifact_type)
            preview = self._artifact_preview(run_dir / filename)
            lines.append(f"{labels[artifact_type]} [{artifact_id}]: {preview}")
            index.append(f"{artifact_id}={artifact_type}")
        lines.append(f"产物索引：{', '.join(index)}")
        return "\n".join(lines)

    @staticmethod
    def _validated_explore_summary(response: str) -> str:
        """Return only a validated Explore conclusion, never raw worker output."""
        try:
            payload = parse_json_output(response or "")
            kind, description = validate_explore_payload(
                payload,
                required_reference_groups=[("findings.md",), ("leads.md",)],
                max_description_chars=EXPLORE_SUMMARY_MAX_CHARS,
                max_description_lines=EXPLORE_SUMMARY_MAX_LINES,
            )
        except Exception as exc:
            raise RunnerFailure(f"invalid Explore summary contract: {exc}") from exc
        if kind != "fact" or not description:
            raise RunnerFailure("invalid Explore summary contract: response was rejected")
        description = redact_sensitive_text(description)
        # Merely pointing at the two files is not a graph Fact. Keep this
        # content-based instead of matching one exact sentence so equivalent
        # variants such as “详见 findings/leads” are rejected as well.
        substantive = description.lower()
        for reference in ("findings.md", "leads.md"):
            substantive = substantive.replace(reference, "")
        substantive_chars = sum(character.isalnum() for character in substantive)
        if substantive_chars < EXPLORE_SUMMARY_MIN_SUBSTANTIVE_CHARS:
            raise RunnerFailure(
                "invalid Explore summary contract: description is only an artifact pointer; "
                "summarize tested scope, observed result, vulnerability conclusion, and leads"
            )
        return description

    def _artifact_fallback_summary(
        self,
        *,
        task,
        task_id: str,
        intent_id: str,
        worker: str,
        run_dir: Path,
        registered_artifacts: dict[str, dict],
        reason: str,
    ) -> str:
        """Build a conservative Fact from durable Explore artifacts.

        Explore writes findings.md and leads.md continuously.  A malformed or
        missing final JSON must not erase those durable results.  This fallback
        never invents a vulnerability: it quotes bounded, normalized previews
        from the two files and explicitly marks the result as partial when the
        files themselves say the round was incomplete.  The output is validated
        through the same contract as a model-produced summary before it can be
        submitted to the graph.
        """
        findings_path = run_dir / "findings.md"
        leads_path = run_dir / "leads.md"
        findings = self._artifact_preview(findings_path, limit=720)
        leads = self._artifact_preview(leads_path, limit=560)
        raw_text = "\n".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in (findings_path, leads_path)
            if path.is_file()
        ).lower()
        # “待验证” is a legitimate conclusion in leads.md: it describes a
        # bounded follow-up direction after the current round.  It must not
        # downgrade an otherwise complete findings/leads pair to a
        # stage-incomplete Fact.  Only markers that describe the artifact
        # itself as unfinished belong in this fallback decision.
        partial_markers = (
            *EXPLORE_UNFINISHED_ARTIFACT_MARKERS,
            "执行中",
            "未完成",
            "in progress",
            "pending",
        )
        if any(marker in raw_text for marker in partial_markers):
            status = "本轮在执行窗口结束时未形成完整最终结论，以下为已落盘的阶段性结果。"
        else:
            status = "本轮未返回可解析的最终 JSON，以下摘要严格依据已落盘产物生成，未新增推断。"
        description = "\n".join(
            (
                status,
                f"findings.md：{findings}",
                f"leads.md：{leads}",
                "证据索引：findings.md、leads.md。",
            )
        )
        payload = {"accepted": True, "data": {"description": description}}
        try:
            _kind, normalized = validate_explore_payload(
                payload,
                required_reference_groups=[("findings.md",), ("leads.md",)],
                max_description_chars=EXPLORE_SUMMARY_MAX_CHARS,
                max_description_lines=EXPLORE_SUMMARY_MAX_LINES,
            )
        except Exception as exc:
            raise RunnerFailure(f"artifact fallback summary failed validation: {exc}") from exc
        assert normalized
        self._audit(
            task,
            worker,
            "explore.summary_fallback",
            f"Explore summary was synthesized from durable artifacts for {intent_id}",
            {
                "intent_id": intent_id,
                "reason": reason,
                "source": "findings.md+leads.md",
                "partial": any(marker in raw_text for marker in partial_markers),
            },
        )
        return normalized

    @staticmethod
    def _explore_summary_repair_prompt(*, run_dir: Path) -> str:
        return (
            "# 仅修复 Explore 最终 JSON\n\n"
            "不要继续测试，不要调用任何工具，不要修改文件，也不要解释。"
            f"已有产物位于 `{run_dir}/findings.md` 和 `{run_dir}/leads.md`。"
            "先读取并综合这两个文件。现在只返回一个符合下列格式的 JSON 对象，"
            "description 必须是简洁但有实质内容的中文摘要，至少包含本轮测试范围、"
            "关键正向或负向结果、是否形成确认漏洞、主要未验证线索，"
            "并引用 findings.md 与 leads.md。禁止只写“结果见两个文件”。"
            "并明确引用 findings.md 与 leads.md：\n"
            '{"accepted":true,"data":{"description":"本次 Intent 的确认结果见 '
            'findings.md，未验证线索见 leads.md"}}'
        )

    def _repair_explore_summary_in_rpc(
        self,
        *,
        rpc,
        driver,
        task,
        worker: WorkerConfig,
        task_id: str,
        intent_id: str,
        run_dir: Path,
        deadline: float,
        usage_parts: list,
    ) -> tuple[str | None, str, object | None]:
        """Repair only the final JSON in the still-warm Execute session.

        This path is intentionally unavailable for tool/provider failures and
        runs only after both Explore artifacts are durable.  It therefore does
        not repeat any penetration-testing work.
        """
        response = ""
        last_result = None
        for attempt in range(1, EXPLORE_SUMMARY_REPAIR_ATTEMPTS + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if not self._task_running(task_id):
                return None, response, last_result
            # JSON-only repair should be fast and must not consume the entire
            # close-out budget. Reserve most of the shared deadline for the
            # independent Artifact/JSON fallback when the warm session is
            # unhealthy or ignores the correction request.
            repair_timeout = max(1, min(30, remaining * 0.2))
            result = rpc.follow_up(
                self._explore_summary_repair_prompt(run_dir=run_dir),
                hard_timeout=repair_timeout,
            )
            last_result = result
            usage_parts.append(extract_token_usage(result.stdout))
            if result.cancelled or not self._task_running(task_id):
                return None, response, result
            response = driver.extract_response_text(result.stdout, result.stderr)
            if result.returncode != 0 and not result.timed_out:
                raise RunnerFailure(
                    result.stderr or response or "Explore summary repair process failed"
                )
            if result.timed_out:
                break
            try:
                summary = self._validated_explore_summary(response)
            except RunnerFailure as exc:
                self._audit(
                    task,
                    worker.name,
                    "explore.summary_repair_invalid",
                    f"Explore same-session summary repair attempt {attempt} was invalid for {intent_id}",
                    {"intent_id": intent_id, "attempt": attempt, "error": str(exc)},
                )
                continue
            self._audit(
                task,
                worker.name,
                "explore.summary_repaired",
                f"Explore summary repaired in the Execute session for {intent_id}",
                {"intent_id": intent_id, "attempt": attempt},
            )
            return summary, response, result
        return None, response, last_result

    def _run_closeout_attempts(
        self,
        *,
        task,
        worker: WorkerConfig,
        task_id: str,
        intent,
        phase: str,
        context: dict,
        run_dir: Path,
        session: str,
        evidence_path: Path | None,
        compact: bool,
        deadline: float,
        lease: LeaseHeartbeat,
        registered_artifacts: dict[str, dict],
        usage_parts: list,
        timeout_callback: Callable[[int], None] | None = None,
    ) -> tuple[ProcessResult | None, str, list[str], str | None, str]:
        """Perform bounded Artifact/JSON close-out attempts under one deadline."""
        last_result: ProcessResult | None = None
        response = ""
        summary: str | None = None
        summary_source = ""
        missing = self._reconcile_required_artifacts(
            task_id=task_id,
            intent_id=intent.id,
            phase=phase,
            worker=worker.name,
            run_dir=run_dir,
            registered_artifacts=registered_artifacts,
        )
        for attempt in range(1, CONCLUDE_MAX_ATTEMPTS + 1):
            if not self._task_running(task_id):
                return last_result, response, missing, summary, summary_source
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            attempts_left = CONCLUDE_MAX_ATTEMPTS - attempt + 1
            # A missing artifact is a file-production problem, not a cheap
            # JSON-repair problem.  Starting a fresh compact Pi process has a
            # non-trivial cold-start cost (model setup + reading the evidence
            # digest + two file writes).  Splitting a 90-second close-out into
            # 3 x 30 seconds made every attempt die before it could write
            # findings.md/leads.md, which then released otherwise recoverable
            # Intents.  Give the first artifact-producing attempt the whole
            # remaining close-out budget.  If it returns early but is still
            # invalid, a later attempt can use whatever budget remains.
            if missing:
                attempt_timeout = max(1, int(remaining))
            else:
                attempt_timeout = max(1, int(remaining / attempts_left))
            if phase == "explore" and not missing:
                prompt = self._explore_summary_repair_prompt(run_dir=run_dir)
            else:
                prompt = render_prompt(
                    load_prompt("v2", f"{phase}_conclude.md"),
                    {
                        "task_id": task_id,
                        "intent_id": intent.id,
                        "intent_description": intent.description,
                        "manifest_path": context["manifest_path"],
                        "run_dir": str(run_dir),
                        "evidence_path": str(evidence_path or ""),
                        "missing_artifacts": ", ".join(missing),
                    },
                )
            result, response = self._conclude(
                worker,
                task_id=task_id,
                phase=phase,
                prompt=prompt,
                session=session,
                timeout=attempt_timeout,
                lease=lease,
                compact=compact,
                timeout_callback=(
                    (lambda timeout=attempt_timeout: timeout_callback(timeout))
                    if timeout_callback is not None
                    else None
                ),
            )
            last_result = result
            usage_parts.append(extract_token_usage(result.stdout))
            if result.cancelled or not self._task_running(task_id):
                return last_result, response, missing, summary, summary_source
            self._materialize_artifact_bundle(run_dir=run_dir, phase=phase)
            missing = self._reconcile_required_artifacts(
                task_id=task_id,
                intent_id=intent.id,
                phase=phase,
                worker=worker.name,
                run_dir=run_dir,
                registered_artifacts=registered_artifacts,
            )
            if result.returncode != 0 and not result.timed_out:
                if phase == "bootstrap" and self._bootstrap_evidence_files(run_dir):
                    self._audit(
                        task,
                        worker.name,
                        "bootstrap.conclude_process_fallback",
                        f"Bootstrap close-out process failed after durable evidence; using conservative fallback for {intent.id}",
                        {
                            "intent_id": intent.id,
                            "attempt": attempt,
                            "returncode": result.returncode,
                            "error": redact_sensitive_text(
                                result.stderr or response or f"{phase} conclude process failed"
                            )[:500],
                        },
                    )
                    return last_result, response, missing, summary, summary_source
                raise RunnerFailure(
                    result.stderr or response or f"{phase} conclude process failed"
                )
            if phase == "explore" and response:
                try:
                    summary = self._validated_explore_summary(response)
                    summary_source = "explore_conclude_summary"
                except RunnerFailure as exc:
                    self._audit(
                        task,
                        worker.name,
                        "explore.conclude_summary_invalid",
                        f"Explore close-out attempt {attempt} returned invalid JSON for {intent.id}",
                        {"intent_id": intent.id, "attempt": attempt, "error": str(exc)},
                    )
            self._audit(
                task,
                worker.name,
                f"{phase}.conclude_completed",
                f"{phase.title()} close-out attempt {attempt} returned",
                {
                    "intent_id": intent.id,
                    "attempt": attempt,
                    "timed_out": result.timed_out,
                    "returncode": result.returncode,
                    "missing_artifacts": missing,
                    "summary_valid": phase != "explore" or summary is not None,
                },
            )
            if not missing and (phase != "explore" or summary is not None):
                return last_result, response, missing, summary, summary_source
            self._audit(
                task,
                worker.name,
                f"{phase}.conclude_retry",
                f"{phase.title()} close-out requires another bounded attempt for {intent.id}",
                {
                    "intent_id": intent.id,
                    "attempt": attempt,
                    "timed_out": result.timed_out,
                    "missing_artifacts": missing,
                    "summary_valid": summary is not None,
                },
            )
        return last_result, response, missing, summary, summary_source

    @staticmethod
    def _artifact_preview(path: Path, *, limit: int = 420) -> str:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")[:131072]
        except OSError:
            return "Registered artifact; content unavailable to the runner."
        fragments: list[str] = []
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("```"):
                continue
            line = line.lstrip("#>*-+ ").strip()
            if not line or set(line) <= {"-", "|", ":", " "}:
                continue
            fragments.append(" ".join(line.split()))
            if len("; ".join(fragments)) >= limit:
                break
        preview = "; ".join(fragments).strip()
        if not preview:
            return "Registered artifact contains no readable prose summary."
        if len(preview) > limit:
            preview = preview[: limit - 1].rstrip() + "…"
        return preview

    def _run_reason(self, task_id: str, worker: WorkerConfig, runtime: RuntimeConfig) -> None:
        driver = get_driver(worker.type)
        if getattr(driver, "supports_rpc", lambda: False)():
            self._run_rpc_reason(task_id, worker, runtime)
            return
        task = self.client.claim_reason(task_id, worker.name)
        lease = LeaseHeartbeat(
            lambda: self.client.heartbeat_reason(task_id, worker.name),
            interval=runtime.heartbeat_interval_seconds,
        )
        try:
            lease.start()
            attempt_id = f"reason-{task_id}-{uuid.uuid4().hex[:10]}"
            context = self.client.build_context(task_id, attempt_id, phase="reason")
            self._report_reason_activity(
                task_id,
                worker.name,
                kind="reading_context",
                summary="正在读取 Facts、Todo 和上一轮 Reason 增量",
            )
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
            open_intent_count = self._open_explore_intent_count(task_id)
            intent_budget = max(
                0,
                runtime.reason_target_open_intents - open_intent_count,
            )
            prompt = render_prompt(
                load_prompt("v2", "reason.md"),
                {
                    "task_id": task_id,
                    "project_id": task.project_id,
                    "origin": task.origin.original_input,
                    "goal": task.goal,
                    "manifest_path": context["manifest_path"],
                    "workspace_id": workspace.id,
                    "open_intent_count": str(open_intent_count),
                    "intent_budget": str(intent_budget),
                },
            )
            self._report_reason_activity(
                task_id,
                worker.name,
                kind="analyzing",
                summary="正在分析 Facts、漏洞证据和 Todo 覆盖情况",
            )
        except Exception as exc:
            lease.stop()
            self._report_reason_activity(
                task_id,
                worker.name,
                kind="failed",
                summary="Reason 初始化失败，等待重新调度",
                best_effort=True,
            )
            if self._task_running(task_id):
                try:
                    self.client.release_reason(task_id, worker.name)
                except Exception:
                    LOG.exception("failed to release reason during initialization task=%s", task_id)
                raise
            return
        usage_parts = []
        try:
            result, _session, response = self._execute(
                worker,
                task_id=task_id,
                phase="reason",
                prompt=prompt,
                timeout=runtime.reason_execute_seconds,
                lease=lease,
            )
            if result.cancelled or not self._task_running(task_id):
                current_workspace = self.client.workspace(workspace.id)
                if current_workspace.status == "open":
                    self.client.abandon_workspace(
                        workspace.id,
                        worker=worker.name,
                        failure_reason=result.cancel_reason or "Task is no longer running",
                    )
                return
            token_usage = extract_token_usage(result.stdout)
            if result.timed_out:
                self._audit(
                    task,
                    worker.name,
                    "reason.execute_budget_exhausted",
                    f"Reason planning budget exhausted for {task_id}",
                    {
                        "workspace_id": workspace.id,
                        "attempt_id": attempt_id,
                        "execute_budget_exhausted": True,
                        "execute_budget_seconds": runtime.reason_execute_seconds,
                    },
                )
            if result.returncode != 0 and not result.timed_out:
                raise RunnerFailure(result.stderr or response or "reason failed")
            self._report_reason_activity(
                task_id,
                worker.name,
                kind="submitting",
                summary="正在提交 Reason 规划结果并更新 Todo/Intent",
            )
            current_task = self.client.task(task_id)
            current_workspace = self.client.workspace(workspace.id)
            if current_task.status != "running":
                if current_workspace.status == "open":
                    self.client.abandon_workspace(
                        workspace.id,
                        worker=worker.name,
                        failure_reason=(
                            "Task completed because Goal was achieved"
                            if current_task.status == "completed"
                            else "Task is no longer running"
                        ),
                    )
                return
            if current_workspace.status != "committed":
                if result.timed_out:
                    raise RunnerFailure(
                        "Reason timeout: planning budget exhausted before Workspace commit"
                    )
                raise RunnerFailure("Reason exited without committing its Workspace")
            if not self._task_running(task_id):
                return
            self.client.release_reason(task_id, worker.name)
            payload = {"workspace_id": workspace.id, "attempt_id": attempt_id}
            payload["execute_budget_exhausted"] = result.timed_out
            if token_usage is not None:
                payload["token_usage"] = token_usage
            self._audit(
                task,
                worker.name,
                "reason.committed",
                f"Reason Workspace {workspace.id} committed",
                payload,
            )
        except Exception as exc:
            self._report_reason_activity(
                task_id,
                worker.name,
                kind="failed",
                summary="Reason 未完成，等待重新调度",
                best_effort=True,
            )
            active = self._task_running(task_id)
            try:
                current = self.client.workspace(workspace.id)
                if current.status == "open":
                    self.client.abandon_workspace(
                        workspace.id,
                        worker=worker.name,
                        failure_reason=(
                            "Task is no longer running"
                            if not active
                            else str(exc)
                        ),
                    )
            except Exception:
                LOG.exception("failed to abandon reason workspace=%s", workspace.id)
            if not active:
                LOG.info(
                    "reason stopped after task became inactive task=%s workspace=%s worker=%s",
                    task_id,
                    workspace.id,
                    worker.name,
                )
                return
            try:
                self.client.release_reason(task_id, worker.name)
            except Exception:
                LOG.exception("failed to release reason task=%s", task_id)
            raise
        finally:
            lease.stop()

    def _report_reason_activity(
        self,
        task_id: str,
        worker: str,
        *,
        kind: str,
        summary: str,
        best_effort: bool = True,
    ) -> None:
        reporter = getattr(self.client, "report_reason_activity", None)
        if reporter is None:
            return
        try:
            reporter(task_id, worker, kind=kind, summary=summary)
        except Exception:
            if not best_effort:
                raise
            LOG.debug("reason activity report failed task=%s kind=%s", task_id, kind, exc_info=True)

    def _run_rpc_reason(self, task_id: str, worker: WorkerConfig, runtime: RuntimeConfig) -> None:
        task = self.client.claim_reason(task_id, worker.name)
        lease = LeaseHeartbeat(
            lambda: self.client.heartbeat_reason(task_id, worker.name),
            interval=runtime.heartbeat_interval_seconds,
        )
        try:
            lease.start()
            attempt_id = f"reason-{task_id}-{uuid.uuid4().hex[:10]}"
            context = self.client.build_context(task_id, attempt_id, phase="reason")
            self._report_reason_activity(
                task_id,
                worker.name,
                kind="reading_context",
                summary="正在读取 Facts、Todo 和上一轮 Reason 增量",
            )
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
            open_intent_count = self._open_explore_intent_count(task_id)
            intent_budget = max(
                0,
                runtime.reason_target_open_intents - open_intent_count,
            )
            prompt = render_prompt(
                load_prompt("v2", "reason.md"),
                {
                    "task_id": task_id,
                    "project_id": task.project_id,
                    "origin": task.origin.original_input,
                    "goal": task.goal,
                    "manifest_path": context["manifest_path"],
                    "workspace_id": workspace.id,
                    "open_intent_count": str(open_intent_count),
                    "intent_budget": str(intent_budget),
                },
            )
            steer_prompt = (
                f"停止继续分析。现在立即提交 Workspace `{workspace.id}`："
                "只保留已经有证据支持的 Todo 和 Intent，完成提交后结束当前 Reason 阶段。"
            )
            prepared_session = get_driver(worker.type).prepare_session()
            rpc = ManagedRpcProcess(
                get_driver(worker.type).build_rpc(worker, prepared_session, phase="reason"),
                worker.env,
            )
            self._track(rpc, task_id)
            self._report_reason_activity(
                task_id,
                worker.name,
                kind="analyzing",
                summary="正在分析 Facts、更新 Todo 并规划下一批 Intent",
            )
        except Exception:
            lease.stop()
            self._report_reason_activity(
                task_id,
                worker.name,
                kind="failed",
                summary="Reason 初始化失败，等待重新调度",
                best_effort=True,
            )
            if self._task_running(task_id):
                try:
                    self.client.release_reason(task_id, worker.name)
                except Exception:
                    LOG.exception("failed to release RPC reason during initialization task=%s", task_id)
                raise
            return
        usage_parts = []
        try:
            rpc.start()
            lease.attach(rpc)
            result = rpc.prompt(
                prompt,
                soft_timeout=runtime.reason_execute_seconds,
                hard_timeout=runtime.reason_execute_seconds + 120,
                steering_message=steer_prompt,
                abort_after_steer=30,
            )
            usage_parts.append(extract_token_usage(result.stdout))
            if result.cancelled or not self._task_running(task_id):
                current_workspace = self.client.workspace(workspace.id)
                if current_workspace.status == "open":
                    self.client.abandon_workspace(
                        workspace.id,
                        worker=worker.name,
                        failure_reason=result.cancel_reason or "Task is no longer running",
                    )
                return
            if result.soft_steered:
                self._audit(
                    task,
                    worker.name,
                    "reason.soft_deadline",
                    f"Reason soft deadline reached; steered Workspace {workspace.id} to commit",
                    {
                        "workspace_id": workspace.id,
                        "attempt_id": attempt_id,
                        "soft_deadline_seconds": runtime.reason_execute_seconds,
                    },
                )
            current_task = self.client.task(task_id)
            current_workspace = self.client.workspace(workspace.id)
            if current_task.status != "running":
                if current_workspace.status == "open":
                    self.client.abandon_workspace(
                        workspace.id,
                        worker=worker.name,
                        failure_reason=(
                            "Task completed because Goal was achieved"
                            if current_task.status == "completed"
                            else "Task is no longer running"
                        ),
                    )
                return
            if current_workspace.status != "committed" and not result.timed_out:
                follow = rpc.follow_up(steer_prompt, hard_timeout=120)
                usage_parts.append(extract_token_usage(follow.stdout))
                if follow.cancelled or not self._task_running(task_id):
                    current_workspace = self.client.workspace(workspace.id)
                    if current_workspace.status == "open":
                        self.client.abandon_workspace(
                            workspace.id,
                            worker=worker.name,
                            failure_reason=follow.cancel_reason or "Task is no longer running",
                        )
                    return
                current_workspace = self.client.workspace(workspace.id)
                if follow.timed_out:
                    result = ProcessResult(
                        returncode=follow.returncode,
                        stdout=follow.stdout,
                        stderr=follow.stderr,
                        timed_out=True,
                        soft_steered=result.soft_steered,
                        soft_aborted=result.soft_aborted,
                    )
            if current_workspace.status != "committed":
                raise RunnerFailure("Reason RPC session exited without committing its Workspace")
            if not self._task_running(task_id):
                return
            self._report_reason_activity(
                task_id,
                worker.name,
                kind="submitting",
                summary="正在提交 Reason 规划结果并更新 Todo/Intent",
            )
            self.client.release_reason(task_id, worker.name)
            payload = {
                "workspace_id": workspace.id,
                "attempt_id": attempt_id,
                "rpc": True,
                "soft_steered": result.soft_steered,
                "soft_aborted": result.soft_aborted,
                "hard_timed_out": result.timed_out,
            }
            token_usage = sum_token_usage(usage_parts)
            if token_usage is not None:
                payload["token_usage"] = token_usage
            self._audit(
                task,
                worker.name,
                "reason.committed",
                f"Reason Workspace {workspace.id} committed",
                payload,
            )
        except Exception as exc:
            self._report_reason_activity(
                task_id,
                worker.name,
                kind="failed",
                summary="Reason 未完成，等待重新调度",
                best_effort=True,
            )
            active = self._task_running(task_id)
            try:
                current = self.client.workspace(workspace.id)
                if current.status == "open":
                    self.client.abandon_workspace(
                        workspace.id,
                        worker=worker.name,
                        failure_reason=(
                            "Task is no longer running"
                            if not active
                            else str(exc)
                        ),
                    )
            except Exception:
                LOG.exception("failed to abandon reason workspace=%s", workspace.id)
            if not active:
                return
            try:
                self.client.release_reason(task_id, worker.name)
            except Exception:
                LOG.exception("failed to release reason task=%s", task_id)
            raise
        finally:
            lease.attach(None)
            lease.stop()
            rpc.stop()
            self._untrack(rpc)

    def _open_explore_intent_count(self, task_id: str) -> int:
        return sum(
            1
            for intent in self.client.intents(task_id)
            if intent.id != "i001"
            and intent.status in {"pending", "claimed", "running", "concluding"}
        )

    def _task_status(self, task_id: str) -> str | None:
        try:
            return self.client.task(task_id).status
        except Exception:
            return None

    def _task_running(self, task_id: str) -> bool:
        return self._task_status(task_id) == "running"

    def _task_completed(self, task_id: str) -> bool:
        return self._task_status(task_id) == "completed"

    def _execute(
        self,
        worker: WorkerConfig,
        *,
        task_id: str | None = None,
        phase: str,
        prompt: str,
        timeout: int,
        lease: LeaseHeartbeat,
    ) -> tuple[ProcessResult, str | None, str]:
        driver = get_driver(worker.type)
        prepared_session = driver.prepare_session()
        command = driver.build_execute(worker, prompt, prepared_session, phase=phase)
        process = ManagedProcess(command.argv, worker.env)
        self._track(process, task_id)
        try:
            process.start()
            lease.attach(process)
            result = process.communicate(timeout=timeout)
        finally:
            lease.attach(None)
            self._untrack(process)
        recover_session = getattr(driver, "recover_session", None)
        if recover_session is None:
            session = driver.extract_session(command.session, result.stdout, result.stderr)
        else:
            session = recover_session(worker, command.session, result.stdout, result.stderr)
        response = driver.extract_response_text(result.stdout, result.stderr)
        return result, session, response

    def _conclude(
        self,
        worker: WorkerConfig,
        *,
        task_id: str | None = None,
        phase: str,
        prompt: str,
        session: str,
        timeout: int,
        lease: LeaseHeartbeat,
        compact: bool = False,
        timeout_callback: Callable[[], None] | None = None,
    ) -> tuple[ProcessResult, str]:
        driver = get_driver(worker.type)
        if compact:
            command = driver.build_compact_conclude(worker, prompt, phase=phase)
        else:
            command = driver.build_conclude(worker, prompt, session, phase=phase)
        process = ManagedProcess(command, worker.env)
        self._track(process, task_id)
        try:
            process.start()
            lease.attach(process)
            result = process.communicate(timeout=timeout, timeout_callback=timeout_callback)
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
        failure_kind = self._failure_kind(exc)
        failure_class = self._failure_class(failure_kind, exc)
        if (
            phase == "bootstrap"
            and failure_class != "transient"
            and intent.retry_count + 1 >= 3
        ):
            failure_kind, failure_class, next_retry = "needs_hint", "planning", None
        else:
            previous_count = (
                intent.infrastructure_retry_count
                if failure_class == "transient"
                else intent.retry_count
            )
            delay = runtime.retry_base_seconds * min(2**previous_count, 8)
            next_retry = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
        try:
            self.client.release_intent(
                task_id,
                intent.id,
                worker,
                failure_kind=failure_kind,
                failure_class=failure_class,
                next_retry_at=next_retry,
            )
        except Exception:
            LOG.exception("failed to release intent task=%s intent=%s", task_id, intent.id)

    @staticmethod
    def _failure_kind(exc: Exception) -> str:
        detail = str(exc).lower()
        if "timeout" in detail or "budget exhausted" in detail:
            return "timeout"
        if "required artifacts missing" in detail:
            return "artifact_contract"
        if "session could not be recovered" in detail:
            return "session_recovery"
        return type(exc).__name__.lower()

    @staticmethod
    def _failure_class(failure_kind: str, exc: Exception) -> str:
        """Classify lifecycle ownership without guessing in the reconciler.

        Transient failures belong to Dispatcher/Worker recovery and therefore
        must not consume the Intent's execution retry budget. Timeouts and
        artifact contract failures describe the execution plan itself and are
        deliberately bounded before Reason is asked to reshape the Intent.
        """
        if failure_kind in {
            "v2protocolerror",
            "session_recovery",
            "stale_lease_recovered",
            "connectionerror",
            "brokenpipeerror",
        }:
            return "transient"
        if failure_kind in {"timeout", "artifact_contract", "needs_hint"}:
            return "planning" if failure_kind == "needs_hint" else "execution"
        if failure_kind == "runnerfailure":
            detail = str(exc).lower()
            if "lease heartbeat failed" in detail:
                return "transient"
            # A non-zero Pi/RPC process exit normally indicates provider,
            # process, or adapter instability. Explicit timeout/artifact cases
            # were normalized above before reaching this branch.
            return "transient"
        return "execution"

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

    def _track(self, process: object, task_id: str | None = None) -> None:
        with self._process_lock:
            self._processes[process] = task_id

    def _untrack(self, process: object) -> None:
        with self._process_lock:
            self._processes.pop(process, None)

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
                "AXIOM_PI_SESSION_DIR": str(run_dir / ".pi-session"),
                "AXIOM_GUARD_ENABLED": "1" if task.guard_enabled else "0",
                "AXIOM_TASK_SCOPE_ENABLED": "0",
            }
        )
        # Resolve recon binaries once at the runner boundary. Skills and
        # models invoke canonical command names from PATH; they must not guess
        # workstation-specific locations such as .workbuddy or Homebrew paths.
        base_path = env.get("PATH") or os.environ.get("PATH", "")
        path_entries = [entry for entry in base_path.split(os.pathsep) if entry]
        resolved_tools: dict[str, str] = {}
        for key, candidates in CANONICAL_CLI_TOOLS.items():
            resolved = None
            for candidate in candidates:
                resolved = shutil.which(candidate, path=base_path)
                if resolved:
                    break
            if not resolved:
                continue
            resolved_tools[key] = resolved
            directory = str(Path(resolved).parent)
            if directory not in path_entries:
                path_entries.insert(0, directory)
        env["PATH"] = os.pathsep.join(path_entries)
        env["AXIOM_TOOL_PATHS_JSON"] = json.dumps(
            resolved_tools,
            ensure_ascii=True,
            separators=(",", ":"),
        )
        for key, resolved in resolved_tools.items():
            env[f"AXIOM_TOOL_{key}"] = resolved
        if intent_id:
            env["AXIOM_INTENT_ID"] = intent_id
        if workspace_id:
            env["AXIOM_REASON_WORKSPACE"] = workspace_id
        Path(env["AXIOM_PI_SESSION_DIR"]).mkdir(parents=True, exist_ok=True)
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
