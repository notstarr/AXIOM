from __future__ import annotations

import logging
import time
from pathlib import Path

from axiom.dispatcher.config import DispatchConfig, WorkerConfig
from axiom.dispatcher.contracts import (
    parse_json_output,
    validate_bootstrap_conclude_payload,
    validate_bootstrap_execute_payload,
)
from axiom.dispatcher.prompting import format_hints, load_prompt, render_prompt
from axiom.dispatcher.protocol.client import CairnClient
from axiom.dispatcher.runtime.cancellation import TaskCancellation
from axiom.dispatcher.runtime.heartbeat import HeartbeatLease
from axiom.dispatcher.tasks.common import (
    _format_blackboard,
    _report_event,
    best_effort_release,
    cancel_reason,
    did_timeout,
    preview,
    project_allows_conclude_fallback,
    record_session_tools,
    report_task_event,
    run_healthcheck,
    run_worker_process,
    task_healthcheck_enabled,
    write_conclude_result,
    write_conclude_result_with_fact_id,
    write_graph_snapshot_file,
)
from axiom.dispatcher.workers.registry import get_driver
from axiom.server.models import Intent, ProjectDetail

LOG = logging.getLogger(__name__)


def run_bootstrap_task(
    config: DispatchConfig,
    client: CairnClient,
    project: ProjectDetail,
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
                "starting local exec project=%s intent=%s worker=%s phase=bootstrap_healthcheck timeout=%ss",
                project.project.id,
                intent.id,
                worker.name,
                healthcheck_timeout,
            )
            healthcheck = run_healthcheck(
                worker,
                driver.build_healthcheck(worker, phase="bootstrap"),
                timeout_seconds=healthcheck_timeout,
                project_id=project.project.id,
                lease=lease,
                cancellation=cancellation,
                client=client,
                task_type="bootstrap",
            )
            cancelled = cancel_reason(healthcheck.result, cancellation)
            if cancelled is not None:
                LOG.info(
                    "bootstrap cancelled during healthcheck project=%s intent=%s worker=%s reason=%s",
                    project.project.id,
                    intent.id,
                    worker.name,
                    cancelled,
                )
                best_effort_release(client, project.project.id, intent.id, worker.name)
                return "cancelled"
            if lease.failure is not None:
                LOG.warning(
                    "heartbeat lost during bootstrap healthcheck project=%s intent=%s worker=%s status=%s",
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

        prompt = render_prompt(
            load_prompt(config.runtime.prompt_group, "bootstrap.md"),
            _bootstrap_prompt_replacements(project),
        )

        session = driver.prepare_session()
        execute = driver.build_execute(worker, prompt, session, phase="bootstrap")
        session = execute.session
        execute_started = time.perf_counter()
        first = run_worker_process(
            worker,
            execute.argv,
            phase="bootstrap",
            timeout_seconds=config.tasks.bootstrap.timeout,
            project_id=project.project.id,
            lease=lease,
            cancellation=cancellation,
            client=client,
            task_type="bootstrap",
        )
        execute_ms = int((time.perf_counter() - execute_started) * 1000)
        session = driver.extract_session(session, first.stdout, first.stderr)
        session = _resolve_valid_session(driver, worker, session)
        try:
            recorded, token_usage = record_session_tools(
                session_id=session or "",
                worker_name=worker.name,
                project_id=project.project.id,
            )
            LOG.info("recorded %d tool executions session=%s worker=%s", recorded, session, worker.name)
            if token_usage:
                _report_event(client, project.project.id, task_type="bootstrap", phase="bootstrap_execute",
                              worker=worker.name, level="info", message="token usage",
                              payload={"token_usage": token_usage})
        except Exception:
            LOG.exception("failed to record tool executions session=%s", session)
        cancelled = cancel_reason(first, cancellation)
        if cancelled is not None:
            LOG.info(
                "bootstrap cancelled project=%s intent=%s worker=%s reason=%s execute_ms=%s",
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
                "heartbeat lost during bootstrap project=%s intent=%s worker=%s status=%s execute_ms=%s",
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
                kind, data = validate_bootstrap_execute_payload(payload)
            except RuntimeError as exc:
                # API-level error (connection error, empty response, safety
                # filter, etc.) — the model produced no usable output.
                # Don't try conclude fallback; just surface the error and release.
                _dump_failed_stdout(project.project.id, intent.id, worker.name, "execute", first.stdout)
                LOG.warning(
                    "bootstrap API error project=%s intent=%s worker=%s error=%s execute_ms=%s total_ms=%s",
                    project.project.id,
                    intent.id,
                    worker.name,
                    exc,
                    execute_ms,
                    int((time.perf_counter() - task_started) * 1000),
                )
                best_effort_release(client, project.project.id, intent.id, worker.name)
                return "api_error"
            except Exception as exc:
                _dump_failed_stdout(project.project.id, intent.id, worker.name, "execute", first.stdout)
                LOG.warning(
                    "bootstrap parse failed project=%s intent=%s worker=%s error=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
                    project.project.id,
                    intent.id,
                    worker.name,
                    exc,
                    execute_ms,
                    int((time.perf_counter() - task_started) * 1000),
                    preview(first.stdout),
                    preview(first.stderr),
                )
                return _try_conclude_fallback(
                    config,
                    client,
                    worker,
                    driver,
                    project,
                    intent,
                    _resolve_valid_session(driver, worker, session),
                    lease,
                    cancellation,
                )
            if kind == "rejected":
                _report_bootstrap_outcome(
                    client,
                    project.project.id,
                    worker.name,
                    conclusion="rejected",
                    phase="bootstrap",
                    detail="worker rejected bootstrap execute payload",
                    extra={"intent_id": intent.id, "execute_ms": execute_ms},
                )
                LOG.warning(
                    "bootstrap rejected project=%s intent=%s worker=%s execute_ms=%s total_ms=%s stdout_preview=%s",
                    project.project.id,
                    intent.id,
                    worker.name,
                    execute_ms,
                    int((time.perf_counter() - task_started) * 1000),
                    preview(first.stdout),
                )
                best_effort_release(client, project.project.id, intent.id, worker.name)
                return "rejected"
            if kind == "fact":
                _report_bootstrap_outcome(
                    client,
                    project.project.id,
                    worker.name,
                    conclusion="fact_written",
                    phase="bootstrap",
                    detail="bootstrap accepted fact-only result",
                    extra={"intent_id": intent.id, "execute_ms": execute_ms},
                )
                return write_conclude_result(
                    client,
                    project.project.id,
                    intent.id,
                    worker.name,
                    data["fact_description"],
                    source="bootstrap",
                    phase_ms=execute_ms,
                    total_ms=int((time.perf_counter() - task_started) * 1000),
                )
            _report_bootstrap_outcome(
                client,
                project.project.id,
                worker.name,
                conclusion="complete",
                phase="bootstrap",
                detail="bootstrap completed goal directly",
                extra={"intent_id": intent.id, "execute_ms": execute_ms},
            )
            return _write_bootstrap_complete_result(
                client,
                project.project.id,
                intent.id,
                worker.name,
                data["fact_description"],
                data["complete_description"],
                source="bootstrap",
                phase_ms=execute_ms,
                total_ms=int((time.perf_counter() - task_started) * 1000),
            )
        if did_timeout(first):
            LOG.warning(
                "bootstrap timed out project=%s intent=%s worker=%s execute_ms=%s total_ms=%s",
                project.project.id,
                intent.id,
                worker.name,
                execute_ms,
                int((time.perf_counter() - task_started) * 1000),
            )
            session = _resolve_valid_session(driver, worker, session)
            fallback = _try_conclude_fallback(
                config, client, worker, driver, project, intent, session, lease, cancellation,
            )
            # Signal the scheduler to cool this worker so another can try.
            return fallback if fallback == "success" else "timed_out"
        LOG.warning(
            "bootstrap command failed project=%s intent=%s worker=%s code=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
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
        LOG.exception("bootstrap task crashed project=%s intent=%s worker=%s", project.project.id, intent.id, worker.name)
        best_effort_release(client, project.project.id, intent.id, worker.name)
        return "failed"
    finally:
        lease.stop()


def _try_conclude_fallback(
    config: DispatchConfig,
    client: CairnClient,
    worker: WorkerConfig,
    driver,
    project: ProjectDetail,
    intent: Intent,
    session: str | None,
    lease: HeartbeatLease,
    cancellation: TaskCancellation,
) -> str:
    if not driver.supports_conclude() or not session:
        LOG.info(
            "bootstrap conclude fallback unavailable project=%s intent=%s worker=%s supports_conclude=%s has_session=%s",
            project.project.id,
            intent.id,
            worker.name,
            driver.supports_conclude(),
            bool(session),
        )
        best_effort_release(client, project.project.id, intent.id, worker.name)
        return "failed"
    if lease.failure is not None:
        LOG.warning(
            "bootstrap conclude fallback skipped because heartbeat already lost project=%s intent=%s worker=%s",
            project.project.id,
            intent.id,
            worker.name,
        )
        best_effort_release(client, project.project.id, intent.id, worker.name)
        return "failed"
    if cancellation.is_cancelled:
        LOG.info(
            "bootstrap conclude fallback skipped because task was cancelled project=%s intent=%s worker=%s reason=%s",
            project.project.id,
            intent.id,
            worker.name,
            cancellation.reason,
        )
        best_effort_release(client, project.project.id, intent.id, worker.name)
        return "cancelled"

    if not project_allows_conclude_fallback(
        client,
        project.project.id,
        worker_name=worker.name,
        intent_id=intent.id,
    ):
        best_effort_release(client, project.project.id, intent.id, worker.name)
        return "failed"

    prompt = render_prompt(
        load_prompt(config.runtime.prompt_group, "bootstrap_conclude.md"),
        _bootstrap_prompt_replacements(project),
    )
    conclude_argv = driver.build_conclude(worker, prompt, session, phase="bootstrap")
    LOG.info("starting bootstrap conclude fallback project=%s intent=%s worker=%s", project.project.id, intent.id, worker.name)
    conclude_started = time.perf_counter()
    result = run_worker_process(
        worker,
        conclude_argv,
        phase="bootstrap_conclude",
        timeout_seconds=config.tasks.bootstrap.conclude_timeout,
        project_id=project.project.id,
        lease=lease,
        cancellation=cancellation,
        client=client,
        task_type="bootstrap",
    )
    conclude_ms = int((time.perf_counter() - conclude_started) * 1000)
    cancelled = cancel_reason(result, cancellation)
    if cancelled is not None:
        LOG.info(
            "bootstrap conclude cancelled project=%s intent=%s worker=%s reason=%s conclude_ms=%s",
            project.project.id,
            intent.id,
            worker.name,
            cancelled,
            conclude_ms,
        )
        best_effort_release(client, project.project.id, intent.id, worker.name)
        return "cancelled"
    if lease.failure is not None:
        best_effort_release(client, project.project.id, intent.id, worker.name)
        return "failed"
    if result.timed_out or result.returncode != 0:
        LOG.warning(
            "bootstrap conclude failed project=%s intent=%s worker=%s code=%s timed_out=%s conclude_ms=%s stdout_preview=%s stderr_preview=%s",
            project.project.id,
            intent.id,
            worker.name,
            result.returncode,
            result.timed_out,
            conclude_ms,
            preview(result.stdout),
            preview(result.stderr),
        )
        best_effort_release(client, project.project.id, intent.id, worker.name)
        return "failed"
    try:
        model_output = driver.extract_response_text(result.stdout, result.stderr)
        payload = parse_json_output(model_output)
        conclude_data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        if isinstance(conclude_data, dict) and isinstance(conclude_data.get("complete"), dict):
            LOG.warning(
                "bootstrap conclude returned unexpected complete payload project=%s intent=%s worker=%s complete_preview=%s",
                project.project.id,
                intent.id,
                worker.name,
                preview(str(conclude_data.get("complete"))),
            )
        kind, fact_description = validate_bootstrap_conclude_payload(payload)
    except RuntimeError:
        LOG.warning(
            "bootstrap conclude API error project=%s intent=%s worker=%s conclude_ms=%s stdout_preview=%s stderr_preview=%s",
            project.project.id,
            intent.id,
            worker.name,
            conclude_ms,
            preview(result.stdout),
            preview(result.stderr),
        )
        best_effort_release(client, project.project.id, intent.id, worker.name)
        return "api_error"
    except Exception as exc:
        LOG.warning(
            "bootstrap conclude parse failed project=%s intent=%s worker=%s error=%s conclude_ms=%s stdout_preview=%s stderr_preview=%s",
            project.project.id,
            intent.id,
            worker.name,
            exc,
            conclude_ms,
            preview(result.stdout),
            preview(result.stderr),
        )
        best_effort_release(client, project.project.id, intent.id, worker.name)
        return "failed"
    if kind == "rejected":
        _report_bootstrap_outcome(
            client,
            project.project.id,
            worker.name,
            conclusion="rejected",
            phase="bootstrap_conclude",
            detail="worker rejected bootstrap conclude fallback",
            extra={"intent_id": intent.id, "conclude_ms": conclude_ms},
        )
        LOG.warning(
            "bootstrap conclude rejected project=%s intent=%s worker=%s conclude_ms=%s stdout_preview=%s",
            project.project.id,
            intent.id,
            worker.name,
            conclude_ms,
            preview(result.stdout),
        )
        best_effort_release(client, project.project.id, intent.id, worker.name)
        return "rejected"
    _report_bootstrap_outcome(
        client,
        project.project.id,
        worker.name,
        conclusion="fact_written",
        phase="bootstrap_conclude",
        detail="bootstrap conclude fallback wrote fact",
        extra={"intent_id": intent.id, "conclude_ms": conclude_ms},
    )
    return write_conclude_result(
        client,
        project.project.id,
        intent.id,
        worker.name,
        fact_description,
        source="bootstrap_conclude",
        phase_ms=conclude_ms,
    )


def _resolve_valid_session(driver, worker: WorkerConfig, session: str | None) -> str | None:
    if not session:
        return _find_latest_session(driver, worker)
    exists_checker = getattr(driver, "session_exists", None)
    if not callable(exists_checker):
        return session
    try:
        if exists_checker(worker, session):
            return session
    except Exception:
        LOG.exception("bootstrap session validation crashed worker=%s session=%s", worker.name, session)
    LOG.warning(
        "bootstrap invalid session id worker=%s session=%s; "
        "falling back to latest worker session (cross-intent risk)",
        worker.name,
        session,
    )
    return _find_latest_session(driver, worker)


def _find_latest_session(driver, worker: WorkerConfig) -> str | None:
    finder = getattr(driver, "find_session_from_files", None)
    if not callable(finder):
        return None
    try:
        return finder(worker.name)
    except Exception:
        LOG.exception("bootstrap session file lookup crashed worker=%s", worker.name)
        return None


def _bootstrap_prompt_replacements(project: ProjectDetail) -> dict[str, str]:
    facts = {fact.id: fact.description for fact in project.facts}
    hints = [
        {
            "id": hint.id,
            "content": hint.content,
            "creator": hint.creator,
            "created_at": hint.created_at,
        }
        for hint in project.hints
    ]
    return {
        "origin": facts.get("origin", ""),
        "goal": facts.get("goal", ""),
        "hints": format_hints(hints) if hints else "No hints provided.",
        "blackboard": _format_blackboard(project.blackboard, project.project.parent_project_id or ""),
    }


def _write_bootstrap_complete_result(
    client: CairnClient,
    project_id: str,
    intent_id: str,
    worker_name: str,
    fact_description: str,
    complete_description: str,
    *,
    source: str,
    phase_ms: int,
    total_ms: int | None = None,
) -> str:
    conclude = write_conclude_result_with_fact_id(
        client,
        project_id,
        intent_id,
        worker_name,
        fact_description,
        source=source,
        phase_ms=phase_ms,
        total_ms=total_ms,
    )
    if conclude.status != "success":
        return "failed"
    if conclude.fact_id is None:
        LOG.warning(
            "bootstrap complete deferred because conclude response omitted fact id project=%s intent=%s worker=%s source=%s",
            project_id,
            intent_id,
            worker_name,
            source,
        )
        return "success"

    response = client.complete(project_id, [conclude.fact_id], complete_description, worker_name)
    if response.status_code in (403, 409):
        LOG.info(
            "bootstrap complete deferred project=%s intent=%s worker=%s source=%s status=%s fact_id=%s",
            project_id,
            intent_id,
            worker_name,
            source,
            response.status_code,
            conclude.fact_id,
        )
        return "success"
    if not response.ok:
        LOG.warning(
            "bootstrap complete write failed project=%s intent=%s worker=%s source=%s fact_id=%s status=%s body=%s",
            project_id,
            intent_id,
            worker_name,
            source,
            conclude.fact_id,
            response.status_code,
            response.text,
        )
        return "success"
    if total_ms is None:
        LOG.info(
            "bootstrap completed project=%s intent=%s worker=%s source=%s from=%s phase_ms=%s",
            project_id,
            intent_id,
            worker_name,
            source,
            [conclude.fact_id],
            phase_ms,
        )
    else:
        LOG.info(
            "bootstrap completed project=%s intent=%s worker=%s source=%s from=%s phase_ms=%s total_ms=%s",
            project_id,
            intent_id,
            worker_name,
            source,
            [conclude.fact_id],
            phase_ms,
            total_ms,
        )
    return "success"


def _dump_failed_stdout(project_id: str, intent_id: str, worker: str, phase: str, stdout: str) -> None:
    dump_dir = Path("/tmp/axiom-debug")
    dump_dir.mkdir(parents=True, exist_ok=True)
    dump_path = dump_dir / f"{project_id}_{intent_id}_{worker}_{phase}.stdout.txt"
    dump_path.write_text(stdout, encoding="utf-8")
    LOG.info("dumped full stdout to %s (%d chars)", dump_path, len(stdout))


def _report_bootstrap_outcome(
    client: CairnClient,
    project_id: str,
    worker_name: str,
    *,
    conclusion: str,
    phase: str,
    detail: str,
    extra: dict[str, object] | None = None,
) -> None:
    payload = {"conclusion": conclusion}
    if extra:
        payload.update(extra)
    report_task_event(
        client,
        project_id,
        task_type="bootstrap",
        phase=phase,
        worker=worker_name,
        level="warn" if conclusion == "rejected" else "info",
        message=detail,
        payload=payload,
    )
