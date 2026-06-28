from __future__ import annotations

import logging
import time

from axiom.dispatcher.config import DispatchConfig, WorkerConfig
from axiom.dispatcher.contracts import parse_json_output, validate_explore_payload
from axiom.dispatcher.prompting import load_prompt, render_prompt
from axiom.dispatcher.protocol.client import CairnClient
from axiom.dispatcher.runtime.cancellation import TaskCancellation
from axiom.dispatcher.runtime.heartbeat import HeartbeatLease
from axiom.dispatcher.tasks.common import (
    _format_blackboard,
    best_effort_release,
    cancel_reason,
    did_timeout,
    preview,
    project_allows_conclude_fallback,
    record_session_tools,
    run_healthcheck,
    run_worker_process,
    task_healthcheck_enabled,
    write_conclude_result,
    write_graph_snapshot_file,
)
from axiom.dispatcher.workers.registry import get_driver
from axiom.server.models import Intent, ProjectDetail

LOG = logging.getLogger(__name__)

# Track per-intent retry count so each retry gets +500s extra timeout.
# Keyed by (project_id, intent_id); cleared on success.
_explore_retries: dict[tuple[str, str], int] = {}
MAX_EXPLORE_RETRIES = 3


def run_explore_task(
    config: DispatchConfig,
    client: CairnClient,
    project: ProjectDetail,
    export_yaml: str,
    intent: Intent,
    worker: WorkerConfig,
    cancellation: TaskCancellation,
) -> str:
    driver = get_driver(worker.type)
    task_started = time.perf_counter()
    healthcheck_timeout = config.runtime.healthcheck_timeout
    lease = HeartbeatLease.for_intent(client, project.project.id, intent.id, worker.name, config.runtime.interval)
    lease.start()
    try:
        if task_healthcheck_enabled(config):
            LOG.info(
                "starting local exec project=%s intent=%s worker=%s phase=explore_healthcheck timeout=%ss",
                project.project.id,
                intent.id,
                worker.name,
                healthcheck_timeout,
            )
            healthcheck = run_healthcheck(
                worker,
                driver.build_healthcheck(worker, phase="explore"),
                timeout_seconds=healthcheck_timeout,
                project_id=project.project.id,
                lease=lease,
                cancellation=cancellation,
                client=client,
                task_type="explore",
            )
            cancelled = cancel_reason(healthcheck.result, cancellation)
            if cancelled is not None:
                LOG.info(
                    "explore cancelled during healthcheck project=%s intent=%s worker=%s reason=%s",
                    project.project.id,
                    intent.id,
                    worker.name,
                    cancelled,
                )
                best_effort_release(client, project.project.id, intent.id, worker.name)
                return "cancelled"
            if lease.failure is not None:
                LOG.warning(
                    "heartbeat lost during explore healthcheck project=%s intent=%s worker=%s status=%s",
                    project.project.id,
                    intent.id,
                    worker.name,
                    lease.failure.status_code,
                )
                best_effort_release(client, project.project.id, intent.id, worker.name)
                return "failed"
            if healthcheck.result.returncode != 0:
                LOG.warning(
                    "worker unhealthy project=%s intent=%s worker=%s healthcheck_ms=%s stderr=%s",
                    project.project.id,
                    intent.id,
                    worker.name,
                    healthcheck.duration_ms,
                    preview(healthcheck.result.stderr),
                )
                best_effort_release(client, project.project.id, intent.id, worker.name)
                return "unhealthy"

        prompt_group = "depth" if project.project.depth_mode else config.runtime.prompt_group
        ai_dir = f"/tmp/axiom-files/{project.project.parent_project_id or 'unknown'}/{project.project.id}/ai"
        checklist_path = f"{ai_dir}/checklist.md"
        sequence_path = f"{ai_dir}/sequence.md"
        prompt = render_prompt(
            load_prompt(prompt_group, "explore.md"),
            {
                "graph_yaml": write_graph_snapshot_file(
                    project.project.id,
                    export_yaml.strip(),
                    phase="explore_execute",
                ),
                "intent_id": intent.id,
                "intent_description": intent.description,
                "ai_dir": ai_dir,
                "checklist_path": checklist_path,
                "sequence_path": sequence_path,
                "blackboard": _format_blackboard(project.blackboard, project.project.parent_project_id or ""),
            },
        )

        session = driver.prepare_session()
        execute = driver.build_execute(worker, prompt, session, phase="explore")
        session = execute.session
        execute_started = time.perf_counter()
        retry_key = (project.project.id, intent.id)
        retries = _explore_retries.get(retry_key, 0)
        if retries >= MAX_EXPLORE_RETRIES:
            LOG.warning(
                "explore max retries reached project=%s intent=%s worker=%s retries=%s",
                project.project.id,
                intent.id,
                worker.name,
                retries,
            )
            best_effort_release(client, project.project.id, intent.id, worker.name)
            return "needs_replan"
        timeout = config.tasks.explore.timeout + (retries * 500)
        _explore_retries[retry_key] = retries + 1
        first = _run_process(
            client,
            project.project.id,
            worker,
            execute.argv,
            phase="explore_execute",
            timeout=timeout,
            lease=lease,
            cancellation=cancellation,
        )
        execute_ms = int((time.perf_counter() - execute_started) * 1000)
        session = driver.extract_session(session, first.stdout, first.stderr)
        try:
            recorded, token_usage = record_session_tools(
                session_id=session or "",
                worker_name=worker.name,
                project_id=project.project.id,
            )
            LOG.info("recorded %d tool executions session=%s worker=%s", recorded, session, worker.name)
            if token_usage:
                _report_event(client, project.project.id, task_type="explore", phase="explore_execute",
                              worker=worker.name, level="info", message="token usage",
                              payload={"token_usage": token_usage})
        except Exception:
            LOG.exception("failed to record tool executions session=%s", session)
        cancelled = cancel_reason(first, cancellation)
        if cancelled is not None:
            LOG.info(
                "explore cancelled project=%s intent=%s worker=%s reason=%s execute_ms=%s",
                project.project.id,
                intent.id,
                worker.name,
                cancelled,
                execute_ms,
            )
            best_effort_release(client, project.project.id, intent.id, worker.name)
            return "cancelled"
        if lease.failure is not None:
            LOG.warning(
                "heartbeat lost during explore project=%s intent=%s worker=%s status=%s execute_ms=%s",
                project.project.id,
                intent.id,
                worker.name,
                lease.failure.status_code,
                execute_ms,
            )
            best_effort_release(client, project.project.id, intent.id, worker.name)
            return "failed"
        if not did_timeout(first) and first.returncode == 0:
            try:
                model_output = driver.extract_response_text(first.stdout, first.stderr)
                payload = parse_json_output(model_output)
                kind, description = validate_explore_payload(payload)
            except RuntimeError:
                LOG.warning(
                    "explore API error project=%s intent=%s worker=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
                    project.project.id,
                    intent.id,
                    worker.name,
                    execute_ms,
                    int((time.perf_counter() - task_started) * 1000),
                    preview(first.stdout),
                    preview(first.stderr),
                )
                best_effort_release(client, project.project.id, intent.id, worker.name)
                return "api_error"
            except Exception as exc:
                LOG.warning(
                    "explore parse failed project=%s intent=%s worker=%s error=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
                    project.project.id,
                    intent.id,
                    worker.name,
                    exc,
                    execute_ms,
                    int((time.perf_counter() - task_started) * 1000),
                    preview(first.stdout),
                    preview(first.stderr),
                )
                fallback = _try_conclude_fallback(
                    config,
                    client,
                    worker,
                    driver,
                    project.project.id,
                    intent,
                    export_yaml,
                    session,
                    lease,
                    cancellation,
                    prompt_group,
                )
                if fallback == "success":
                    _explore_retries.pop(retry_key, None)
                return fallback
            if kind == "rejected":
                LOG.warning(
                    "explore rejected project=%s intent=%s worker=%s execute_ms=%s total_ms=%s stdout_preview=%s",
                    project.project.id,
                    intent.id,
                    worker.name,
                    execute_ms,
                    int((time.perf_counter() - task_started) * 1000),
                    preview(first.stdout),
                )
                best_effort_release(client, project.project.id, intent.id, worker.name)
                return "rejected"
            _explore_retries.pop(retry_key, None)
            return write_conclude_result(
                client,
                project.project.id,
                intent.id,
                worker.name,
                description,
                source="explore_execute",
                phase_ms=execute_ms,
                total_ms=int((time.perf_counter() - task_started) * 1000),
            )
        if did_timeout(first):
            LOG.warning(
                "explore timed out project=%s intent=%s worker=%s execute_ms=%s total_ms=%s",
                project.project.id,
                intent.id,
                worker.name,
                execute_ms,
                int((time.perf_counter() - task_started) * 1000),
            )
            if not session:
                session = driver.find_session_from_files(worker.name)
            fallback = _try_conclude_fallback(
                config, client, worker, driver, project.project.id, intent,
                export_yaml, session, lease, cancellation, prompt_group,
            )
            if fallback == "success":
                _explore_retries.pop(retry_key, None)
                return "success"
            return "timed_out"
        LOG.warning(
            "explore command failed project=%s intent=%s worker=%s code=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
            project.project.id,
            intent.id,
            worker.name,
            first.returncode,
            execute_ms,
            int((time.perf_counter() - task_started) * 1000),
            preview(first.stdout),
            preview(first.stderr),
        )
        best_effort_release(client, project.project.id, intent.id, worker.name)
        return "failed"
    except Exception:
        LOG.exception("explore task crashed project=%s intent=%s worker=%s", project.project.id, intent.id, worker.name)
        best_effort_release(client, project.project.id, intent.id, worker.name)
        return "failed"
    finally:
        lease.stop()


def _try_conclude_fallback(
    config: DispatchConfig,
    client: CairnClient,
    worker: WorkerConfig,
    driver,
    project_id: str,
    intent: Intent,
    export_yaml: str,
    session: str | None,
    lease: HeartbeatLease,
    cancellation: TaskCancellation,
    prompt_group: str,
) -> str:
    if not driver.supports_conclude() or not session:
        LOG.info(
            "conclude fallback unavailable project=%s intent=%s worker=%s supports_conclude=%s has_session=%s",
            project_id,
            intent.id,
            worker.name,
            driver.supports_conclude(),
            bool(session),
        )
        best_effort_release(client, project_id, intent.id, worker.name)
        return "failed"
    if lease.failure is not None:
        LOG.warning("conclude fallback skipped because heartbeat already lost project=%s intent=%s worker=%s", project_id, intent.id, worker.name)
        best_effort_release(client, project_id, intent.id, worker.name)
        return "failed"
    if cancellation.is_cancelled:
        LOG.info(
            "conclude fallback skipped because task was cancelled project=%s intent=%s worker=%s reason=%s",
            project_id,
            intent.id,
            worker.name,
            cancellation.reason,
        )
        best_effort_release(client, project_id, intent.id, worker.name)
        return "cancelled"

    if not project_allows_conclude_fallback(
        client,
        project_id,
        worker_name=worker.name,
        intent_id=intent.id,
    ):
        best_effort_release(client, project_id, intent.id, worker.name)
        return "failed"

    prompt = render_prompt(
        load_prompt(prompt_group, "explore_conclude.md"),
        {
            "graph_yaml": write_graph_snapshot_file(
                project_id,
                export_yaml.strip(),
                phase="explore_conclude",
            ),
            "intent_id": intent.id,
            "intent_description": intent.description,
        },
    )
    conclude_argv = driver.build_conclude(worker, prompt, session, phase="explore")
    LOG.info("starting conclude fallback project=%s intent=%s worker=%s", project_id, intent.id, worker.name)
    conclude_started = time.perf_counter()
    result = _run_process(
        client,
        project_id,
        worker,
        conclude_argv,
        phase="explore_conclude",
        timeout=config.tasks.explore.conclude_timeout,
        lease=lease,
        cancellation=cancellation,
    )
    conclude_ms = int((time.perf_counter() - conclude_started) * 1000)
    cancelled = cancel_reason(result, cancellation)
    if cancelled is not None:
        LOG.info(
            "conclude cancelled project=%s intent=%s worker=%s reason=%s conclude_ms=%s",
            project_id,
            intent.id,
            worker.name,
            cancelled,
            conclude_ms,
        )
        best_effort_release(client, project_id, intent.id, worker.name)
        return "cancelled"
    if lease.failure is not None:
        best_effort_release(client, project_id, intent.id, worker.name)
        return "failed"
    if result.timed_out or result.returncode != 0:
        LOG.warning(
            "conclude failed project=%s intent=%s worker=%s code=%s timed_out=%s conclude_ms=%s stdout_preview=%s stderr_preview=%s",
            project_id,
            intent.id,
            worker.name,
            result.returncode,
            result.timed_out,
            conclude_ms,
            preview(result.stdout),
            preview(result.stderr),
        )
        best_effort_release(client, project_id, intent.id, worker.name)
        return "failed"
    try:
        model_output = driver.extract_response_text(result.stdout, result.stderr)
        payload = parse_json_output(model_output)
        kind, description = validate_explore_payload(payload)
    except RuntimeError:
        LOG.warning(
            "explore conclude API error project=%s intent=%s worker=%s conclude_ms=%s stdout_preview=%s stderr_preview=%s",
            project_id,
            intent.id,
            worker.name,
            conclude_ms,
            preview(result.stdout),
            preview(result.stderr),
        )
        best_effort_release(client, project_id, intent.id, worker.name)
        return "api_error"
    except Exception as exc:
        LOG.warning(
            "conclude parse failed project=%s intent=%s worker=%s error=%s conclude_ms=%s stdout_preview=%s stderr_preview=%s",
            project_id,
            intent.id,
            worker.name,
            exc,
            conclude_ms,
            preview(result.stdout),
            preview(result.stderr),
        )
        best_effort_release(client, project_id, intent.id, worker.name)
        return "failed"
    if kind == "rejected":
        LOG.warning(
            "conclude rejected project=%s intent=%s worker=%s conclude_ms=%s stdout_preview=%s",
            project_id,
            intent.id,
            worker.name,
            conclude_ms,
            preview(result.stdout),
        )
        best_effort_release(client, project_id, intent.id, worker.name)
        return "rejected"
    return write_conclude_result(
        client,
        project_id,
        intent.id,
        worker.name,
        description,
        source="explore_conclude",
        phase_ms=conclude_ms,
    )


def _run_process(
    client: CairnClient,
    project_id: str,
    worker: WorkerConfig,
    argv: list[str],
    *,
    phase: str,
    timeout: int,
    lease: HeartbeatLease,
    cancellation: TaskCancellation,
):
    return run_worker_process(
        worker,
        argv,
        phase=phase,
        timeout_seconds=timeout,
        project_id=project_id,
        lease=lease,
        cancellation=cancellation,
        client=client,
        task_type="explore",
    )
