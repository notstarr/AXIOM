from __future__ import annotations

import logging
import time

from axiom.dispatcher.config import DispatchConfig, WorkerConfig
from axiom.dispatcher.contracts import parse_json_output
from axiom.dispatcher.prompting import load_prompt, render_prompt
from axiom.dispatcher.protocol.client import CairnClient
from axiom.dispatcher.runtime.cancellation import TaskCancellation
from axiom.dispatcher.runtime.heartbeat import HeartbeatLease
from axiom.dispatcher.tasks.common import (
    _format_blackboard,
    _report_event,
    best_effort_release,
    cancel_reason,
    preview,
    record_session_tools,
    run_worker_process,
    task_healthcheck_enabled,
    write_conclude_result,
)
from axiom.dispatcher.workers.registry import get_driver
from axiom.server.models import Intent, ProjectDetail

LOG = logging.getLogger(__name__)


def run_plan_task(
    config: DispatchConfig,
    client: CairnClient,
    project: ProjectDetail,
    intent: Intent,
    worker: WorkerConfig,
    cancellation: TaskCancellation,
) -> str:
    driver = get_driver(worker.type)
    task_started = time.perf_counter()
    lease = HeartbeatLease.for_intent(
        client, project.project.id, intent.id, worker.name, config.runtime.interval
    )
    lease.start()
    try:
        prompt_group = "depth" if project.project.depth_mode else config.runtime.prompt_group
        import os as _os
        parent = project.project.parent_project_id or "unknown"
        checklist_dir = f"/tmp/axiom-files/{parent}/{project.project.id}/ai"
        checklist_path = f"{checklist_dir}/checklist.md"
        sequence_path = f"{checklist_dir}/sequence.md"
        _os.makedirs(checklist_dir, exist_ok=True)
        prompt = render_prompt(
            load_prompt(prompt_group, "plan.md"),
            {
                "origin": project.facts[0].description if project.facts else "",
                "checklist_path": checklist_path,
                "sequence_path": sequence_path,
                "blackboard": _format_blackboard(project.blackboard, project.project.parent_project_id or ""),
            },
        )

        session = driver.prepare_session()
        execute = driver.build_execute(worker, prompt, session, phase="plan")
        session = execute.session
        execute_started = time.perf_counter()
        result = run_worker_process(
            worker,
            execute.argv,
            phase="plan",
            timeout_seconds=config.tasks.plan.timeout,
            project_id=project.project.id,
            lease=lease,
            cancellation=cancellation,
            client=client,
            task_type="plan",
        )
        execute_ms = int((time.perf_counter() - execute_started) * 1000)
        session = driver.extract_session(session, result.stdout, result.stderr)
        try:
            _, token_usage = record_session_tools(session_id=session or "", worker_name=worker.name, project_id=project.project.id)
            if token_usage:
                _report_event(client, project.project.id, task_type="plan", phase="plan",
                              worker=worker.name, level="info", message="token usage",
                              payload={"token_usage": token_usage})
        except Exception:
            LOG.exception("failed to record tool executions session=%s", session)
        cancelled = cancel_reason(result, cancellation)
        if cancelled is not None:
            LOG.info("plan cancelled project=%s worker=%s", project.project.id, worker.name)
            best_effort_release(client, project.project.id, intent.id, worker.name)
            return "cancelled"
        if lease.failure is not None:
            best_effort_release(client, project.project.id, intent.id, worker.name)
            return "failed"
        if result.returncode != 0:
            LOG.warning("plan command failed project=%s worker=%s code=%s", project.project.id, worker.name, result.returncode)
            best_effort_release(client, project.project.id, intent.id, worker.name)
            return "failed"
        try:
            model_output = driver.extract_response_text(result.stdout, result.stderr)
            payload = parse_json_output(model_output)
            if not payload.get("accepted", False):
                LOG.warning("plan rejected project=%s worker=%s", project.project.id, worker.name)
                best_effort_release(client, project.project.id, intent.id, worker.name)
                return "rejected"
        except RuntimeError:
            LOG.warning("plan API error project=%s worker=%s", project.project.id, worker.name)
            best_effort_release(client, project.project.id, intent.id, worker.name)
            return "api_error"
        except Exception:
            LOG.warning("plan parse failed project=%s worker=%s", project.project.id, worker.name)
            best_effort_release(client, project.project.id, intent.id, worker.name)
            return "failed"
        # Write one summary fact pointing to the checklist and sequence diagram.
        try:
            lines = len(open(checklist_path).readlines()) if _os.path.exists(checklist_path) else 0
        except Exception:
            lines = 0
        has_seq = 1 if _os.path.exists(sequence_path) else 0
        summary = f"Plan: checklist ({lines} lines) → {checklist_path}"
        if has_seq:
            summary += f", sequence → {sequence_path}"
        write_conclude_result(
            client, project.project.id, intent.id, worker.name,
            summary, source="plan", phase_ms=execute_ms,
            total_ms=int((time.perf_counter() - task_started) * 1000),
        )
        LOG.info("plan wrote checklist project=%s worker=%s path=%s", project.project.id, worker.name, checklist_path)
        return "success"
    except Exception:
        LOG.exception("plan task crashed project=%s worker=%s", project.project.id, worker.name)
        best_effort_release(client, project.project.id, intent.id, worker.name)
        return "failed"
    finally:
        lease.stop()
