from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
import json
import requests

from axiom.dispatcher.config import DispatchConfig, WorkerConfig
from axiom.dispatcher.contracts import parse_json_output
from axiom.dispatcher.prompting import load_prompt, render_prompt
from axiom.dispatcher.protocol.client import CairnClient
from axiom.dispatcher.runtime.cancellation import TaskCancellation
from axiom.dispatcher.tasks.common import (
    best_effort_release,
    cancel_reason,
    did_timeout,
    preview,
    record_session_tools,
    run_healthcheck,
    run_worker_process,
    task_healthcheck_enabled,
    write_graph_snapshot_file,
    _report_event,
)
from axiom.dispatcher.workers.registry import get_driver
from axiom.server.models import ProjectDetail

LOG = logging.getLogger(__name__)
MAINTAINER_TIMEOUT = 180
MAINTAINER_RETRY_DELAY = 10

# Mozzie vulnerability platform
MOZZIE_URL = "https://mozzie.t.cn-shenzhen.aliyun.kkgroup.work"
MOZZIE_TOKEN = os.getenv("MOZZIE_TOKEN", "")


def _parse_iso8601(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def _evaluate_mozzie_tool_calls(
    tool_items: list[dict],
    *,
    started_at: datetime,
    expected_reports: int,
) -> tuple[bool, str]:
    if expected_reports <= 0:
        return True, "no external reports expected"

    endpoint = "mozzie.t.cn-shenzhen.aliyun.kkgroup.work/api/public/vulnerabilities"
    matched: list[dict] = []
    for item in tool_items:
        argv = " ".join(item.get("argv") or [])
        if endpoint not in argv:
            continue
        created_at = _parse_iso8601(item.get("created_at"))
        if created_at is None or created_at < started_at:
            continue
        matched.append(item)

    if not matched:
        return False, "no Mozzie POST call found after maintainer started"

    success = 0
    failures: list[str] = []
    for item in matched:
        item_id = item.get("id", "unknown")
        status = item.get("status")
        rc = item.get("returncode")
        if status != "success" or rc not in (0, None):
            failures.append(f"{item_id}: shell status={status} rc={rc}")
            continue
        stdout = (item.get("stdout") or "").strip()
        if not stdout:
            failures.append(f"{item_id}: empty stdout from Mozzie POST")
            continue
        try:
            payload = json.loads(stdout)
        except Exception:
            failures.append(f"{item_id}: non-json stdout {stdout[:120]}")
            continue
        if payload.get("success") is True:
            success += 1
        else:
            failures.append(f"{item_id}: mozzie rejected {payload}")

    if failures:
        return False, "; ".join(failures[:3])
    if success < expected_reports:
        return False, f"mozzie success count {success} < expected {expected_reports}"
    return True, f"mozzie success count={success}"


def _verify_external_reports(
    *,
    server_url: str,
    project_id: str,
    started_at: datetime,
    expected_reports: int,
) -> tuple[bool, str]:
    if expected_reports <= 0:
        return True, "no external reports expected"
    try:
        response = requests.get(
            f"{server_url}/projects/{project_id}/tool-executions",
            params={"limit": 2000},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        items = payload.get("items", []) if isinstance(payload, dict) else []
        if not isinstance(items, list):
            items = []
    except Exception as exc:
        return False, f"cannot load tool executions for verification: {exc}"
    return _evaluate_mozzie_tool_calls(
        items,
        started_at=started_at,
        expected_reports=expected_reports,
    )


def run_maintainer_task(
    config: DispatchConfig,
    client: CairnClient,
    worker: WorkerConfig,
    project: ProjectDetail,
    export_yaml: str,
    cancellation: TaskCancellation,
    maintainer_context: dict,
) -> str:
    """Run an asset maintenance task after an explore intent concludes.
    
    maintainer_context must contain:
        - intent_id: the intent that just concluded
        - findings_path: path to the findings MD file
    """
    intent_id = maintainer_context.get("intent_id", "unknown")
    findings_path = maintainer_context.get("findings_path", "")
    group_id = maintainer_context.get("group_id", "")
    proj_id = project.project.id

    if not findings_path.strip():
        LOG.info("maintainer skipped: no findings_path project=%s intent=%s", proj_id, intent_id)
        return "skipped"

    driver = get_driver(worker.type)
    if driver is None:
        LOG.warning("maintainer no driver for worker type=%s", worker.type)
        return "failed"

    task_started = time.perf_counter()
    run_started_at = datetime.now(timezone.utc)

    # Healthcheck (lightweight, just a ping)
    try:
        healthcheck_enabled = task_healthcheck_enabled(config, "maintainer")
    except Exception:
        healthcheck_enabled = True
    if healthcheck_enabled:
        healthcheck_timeout = 15
        health_check = run_healthcheck(
            worker,
            driver.build_healthcheck(worker, phase="maintainer"),
            timeout_seconds=healthcheck_timeout,
            project_id=proj_id,
            task_type="maintainer",
        )
        if health_check.result.returncode != 0:
            LOG.warning("maintainer healthcheck failed project=%s worker=%s", proj_id, worker.name)
            return "unhealthy"

    prompt_group = config.runtime.prompt_group
    ai_dir = f"/tmp/axiom-files/{project.project.parent_project_id or 'unknown'}/{proj_id}/ai"
    server_url = f"http://localhost:8888"

    prompt = render_prompt(
        load_prompt(prompt_group, "maintainer.md"),
        {
            "findings_path": findings_path,
            "project_id": proj_id,
            "group_id": group_id,
            "server_url": server_url,
            "mozzie_url": MOZZIE_URL,
            "mozzie_token": MOZZIE_TOKEN,
        },
    )

    session = driver.prepare_session()
    command = driver.build_execute(worker, prompt, session, phase="maintainer")
    execute_started = time.perf_counter()

    # Report start with intent_id for spinner tracking
    _report_event(client, proj_id, task_type="maintainer",
                  phase="maintainer_execute", worker=worker.name, level="info",
                  message="phase started", payload={"intent_id": intent_id})

    result = run_worker_process(
        worker,
        command.argv,
        phase="maintainer_execute",
        timeout_seconds=MAINTAINER_TIMEOUT,
        project_id=proj_id,
        cancellation=cancellation,
        client=client,
        task_type="maintainer",
    )
    execute_ms = int((time.perf_counter() - execute_started) * 1000)

    # Report finish so spinner stops
    _report_event(client, proj_id, task_type="maintainer",
                  phase="maintainer_execute", worker=worker.name, level="info",
                  message="phase finished", payload={"intent_id": intent_id})
    session = driver.extract_session(session, result.stdout, result.stderr)

    try:
        recorded, token_usage = record_session_tools(
            session_id=session or "",
            worker_name=worker.name,
            project_id=proj_id,
        )
        LOG.info("maintainer recorded %d tool calls session=%s worker=%s", recorded, session, worker.name)
    except Exception:
        LOG.exception("maintainer failed to record tools session=%s", session)

    if cancellation.is_cancelled:
        LOG.info("maintainer cancelled project=%s intent=%s reason=%s", proj_id, intent_id, cancel_reason(cancellation))
        return "cancelled"

    if not did_timeout(result) and result.returncode == 0:
        payload = parse_json_output(result.stdout)
        if payload and payload.get("accepted"):
            data = payload.get("data", {})
            expected_reports = int(data.get("vulns_reported", 0) or 0)
            verified, reason = _verify_external_reports(
                server_url=server_url,
                project_id=proj_id,
                started_at=run_started_at,
                expected_reports=expected_reports,
            )
            if not verified:
                LOG.warning(
                    "maintainer external report verification failed project=%s intent=%s reason=%s",
                    proj_id,
                    intent_id,
                    reason,
                )
                return _retry_maintainer(config, client, project, export_yaml, cancellation, proj_id,
                                         intent_id, findings_path, worker, driver)
            LOG.info(
                "maintainer completed project=%s intent=%s assets=%s vulns=%s execute_ms=%s",
                proj_id, intent_id,
                data.get("assets_count", 0),
                data.get("vulns_reported", 0),
                execute_ms,
            )
            return "success"
        LOG.warning("maintainer unexpected JSON project=%s intent=%s stdout=%s", proj_id, intent_id, preview(result.stdout))
        return _retry_maintainer(config, client, project, export_yaml, cancellation, proj_id,
                                 intent_id, findings_path, worker, driver)

    if did_timeout(result):
        LOG.warning("maintainer timed out project=%s intent=%s execute_ms=%s", proj_id, intent_id, execute_ms)
        return _retry_maintainer(config, client, project, export_yaml, cancellation, proj_id,
                                 intent_id, findings_path, worker, driver)

    LOG.warning("maintainer failed project=%s intent=%s code=%s stderr=%s", proj_id, intent_id, result.returncode, preview(result.stderr))
    return _retry_maintainer(config, client, project, export_yaml, cancellation, proj_id,
                             intent_id, findings_path, worker, driver)


def _retry_maintainer(
    config, client, project, export_str, cancellation, proj_id, intent_id,
    findings_path, worker, driver,
) -> str:
    import time as time_mod
    time_mod.sleep(MAINTAINER_RETRY_DELAY)
    LOG.info("maintainer retry project=%s intent=%s after=%ds", proj_id, intent_id, MAINTAINER_RETRY_DELAY)

    # Re-render prompt (fresh state)
    prompt_group = config.runtime.prompt_group
    run_started_at = datetime.now(timezone.utc)
    retry_group_id = project.project.parent_project_id or ""
    prompt = render_prompt(
        load_prompt(prompt_group, "maintainer.md"),
        {
            "findings_path": findings_path,
            "project_id": proj_id,
            "group_id": retry_group_id,
            "server_url": f"http://localhost:8888",
            "mozzie_url": MOZZIE_URL,
            "mozzie_token": MOZZIE_TOKEN,
        },
    )

    session = driver.prepare_session()
    command = driver.build_execute(worker, prompt, session, phase="maintainer_retry")
    _report_event(client, proj_id, task_type="maintainer",
                  phase="maintainer_retry", worker=worker.name, level="info",
                  message="phase started", payload={"intent_id": intent_id})

    result = run_worker_process(
        worker,
        command.argv,
        phase="maintainer_retry",
        timeout_seconds=MAINTAINER_TIMEOUT,
        project_id=proj_id,
        cancellation=cancellation,
        client=client,
        task_type="maintainer",
    )

    _report_event(client, proj_id, task_type="maintainer",
                  phase="maintainer_retry", worker=worker.name, level="info",
                  message="phase finished", payload={"intent_id": intent_id})

    if not did_timeout(result) and result.returncode == 0:
        payload = parse_json_output(result.stdout)
        if payload and payload.get("accepted"):
            data = payload.get("data", {})
            expected_reports = int(data.get("vulns_reported", 0) or 0)
            verified, reason = _verify_external_reports(
                server_url=f"http://localhost:8888",
                project_id=proj_id,
                started_at=run_started_at,
                expected_reports=expected_reports,
            )
            if not verified:
                LOG.warning(
                    "maintainer retry external report verification failed project=%s intent=%s reason=%s",
                    proj_id,
                    intent_id,
                    reason,
                )
                return "failed"
            LOG.info(
                "maintainer retry completed project=%s intent=%s assets=%s vulns=%s",
                proj_id, intent_id,
                data.get("assets_count", 0),
                data.get("vulns_reported", 0),
            )
            return "success"

    if did_timeout(result):
        LOG.warning("maintainer retry timed out project=%s intent=%s", proj_id, intent_id)
    else:
        LOG.warning("maintainer retry failed project=%s intent=%s code=%s", proj_id, intent_id, result.returncode)
    return "failed"
