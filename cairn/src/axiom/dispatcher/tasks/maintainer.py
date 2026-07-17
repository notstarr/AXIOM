from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
import ast
import json
import re
import requests
import uuid

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


def _extract_mozzie_success_payload(stdout: str) -> dict | None:
    text = (stdout or "").strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
        return payload if isinstance(payload, dict) else None
    except Exception:
        pass
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        candidate = text[match.start():].strip()
        try:
            payload, _ = decoder.raw_decode(candidate)
        except Exception:
            continue
        if isinstance(payload, dict) and "success" in payload:
            return payload
    status_match = re.search(r"\bstatus_code\s*:\s*(\d{3})\b", text, flags=re.I)
    success_match = re.search(r"\bsuccess\s*:\s*(true|false)\b", text, flags=re.I)
    if status_match and success_match:
        status_code = int(status_match.group(1))
        return {
            "success": success_match.group(1).lower() == "true" and 200 <= status_code < 300,
            "status_code": status_code,
            "raw": text[:500],
        }
    return None


def _extract_reported_curl_commands(item: dict) -> list[str]:
    parts: list[str] = []
    argv = item.get("argv") or []
    if isinstance(argv, list):
        parts.extend(str(part) for part in argv)
    else:
        parts.append(str(argv))
    parts.append(str(item.get("stdout") or ""))
    text = "\n".join(parts)
    curls: list[str] = []

    for pattern in (
        r'"curl_command"\s*:\s*"((?:[^"\\]|\\.)*)"',
        r'"curl_command"\s*:\s*\'((?:[^\'\\]|\\.)*)\'',
        r"'curl_command'\s*:\s*'((?:[^'\\]|\\.)*)'",
        r"'curl_command'\s*:\s*\"((?:[^\"\\]|\\.)*)\"",
        r'curl_command\s*=\s*"((?:[^"\\]|\\.)*)"',
        r"curl_command\s*=\s*'((?:[^'\\]|\\.)*)'",
    ):
        for match in re.finditer(pattern, text, flags=re.S):
            raw = match.group(1)
            quote = '"' if '"' in pattern[:20] else "'"
            try:
                curls.append(ast.literal_eval(quote + raw + quote))
            except Exception:
                try:
                    curls.append(json.loads('"' + raw + '"'))
                except Exception:
                    curls.append(raw)
    return [curl for curl in curls if isinstance(curl, str) and curl.strip()]


def _validate_reported_curl_command(curl_command: str) -> tuple[bool, str]:
    curl = (curl_command or "").strip()
    compact = " ".join(curl.split())
    if len(compact) < 80:
        return False, "curl_command too short"
    if not compact.startswith("curl "):
        return False, "curl_command must start with curl"
    if "http://" not in compact and "https://" not in compact:
        return False, "curl_command missing URL"
    if re.search(r"(?:^|\s)-(?:X|H|d)\s*\\?$", compact) or compact.endswith("\\"):
        return False, "curl_command appears truncated"
    if "<" in compact and ">" in compact:
        return False, "curl_command contains placeholder values"
    if "..." in compact or "省略" in compact:
        return False, "curl_command contains ellipsis/omission"
    method_match = re.search(r"\s-X\s+([A-Z]+)", compact)
    method = method_match.group(1) if method_match else "GET"
    if method in {"POST", "PUT", "PATCH", "DELETE"} and not any(token in compact for token in (" -d ", " --data", "--data-raw", "--data-urlencode")):
        return False, f"{method} curl_command missing request body"
    if ("/client/app" in compact or "kkguan.com" in compact) and "Content-Type: application/x-www-form-urlencoded" not in compact:
        return False, "miniapp curl_command missing form content-type"
    return True, "ok"


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
        stdout_text = str(item.get("stdout") or "")
        if endpoint not in argv and endpoint not in stdout_text and "curl_command" not in stdout_text:
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
        payload = _extract_mozzie_success_payload(stdout)
        if payload is None:
            failures.append(f"{item_id}: non-json stdout {stdout[:120]}")
            continue
        if payload.get("success") is True:
            curls = _extract_reported_curl_commands(item)
            if not curls:
                failures.append(f"{item_id}: cannot verify curl_command from Mozzie POST payload")
                continue
            invalid = []
            for curl in curls:
                ok, reason = _validate_reported_curl_command(curl)
                if not ok:
                    invalid.append(reason)
            if invalid:
                failures.append(f"{item_id}: invalid curl_command: {invalid[0]}")
                continue
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
    external_report_enabled = bool(getattr(project.project, "kk_report_enabled", True))

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
            "external_report_enabled": "true" if external_report_enabled else "false",
        },
    )

    session = driver.prepare_session()
    command = driver.build_execute(worker, prompt, session, phase="maintainer")
    session = command.session
    maintainer_metadata = {
        "logical_thread_id": f"maintainer:{proj_id}:{intent_id}",
        "attempt_id": f"maintainer:{proj_id}:{intent_id}:{worker.name}:{uuid.uuid4().hex[:8]}",
        "attempt_role": "maintainer",
        "intent_id": intent_id,
        "session_id": session or "",
    }
    execute_started = time.perf_counter()

    result = run_worker_process(
        worker,
        command.argv,
        phase="maintainer_execute",
        timeout_seconds=MAINTAINER_TIMEOUT,
        project_id=proj_id,
        cancellation=cancellation,
        client=client,
        task_type="maintainer",
        event_metadata=maintainer_metadata,
    )
    execute_ms = int((time.perf_counter() - execute_started) * 1000)
    session = driver.extract_session(session, result.stdout, result.stderr)

    try:
        recorded, token_usage = record_session_tools(
            session_id=session or "",
            worker_name=worker.name,
            project_id=proj_id,
            task_type="maintainer",
            phase="maintainer_execute",
            event_metadata={**maintainer_metadata, "session_id": session or maintainer_metadata.get("session_id", "")},
        )
        LOG.info("maintainer recorded %d tool calls session=%s worker=%s", recorded, session, worker.name)
        if token_usage:
            _report_event(client, proj_id, task_type="maintainer", phase="maintainer_execute",
                          worker=worker.name, level="info", message="token usage",
                          payload={"token_usage": token_usage, **maintainer_metadata, "session_id": session or maintainer_metadata.get("session_id", "")})
    except Exception:
        LOG.exception("maintainer failed to record tools session=%s", session)

    if cancellation.is_cancelled:
        LOG.info("maintainer cancelled project=%s intent=%s reason=%s", proj_id, intent_id, cancel_reason(cancellation))
        return "cancelled"

    if not did_timeout(result) and result.returncode == 0:
        payload = parse_json_output(result.stdout)
        if payload and payload.get("accepted"):
            data = payload.get("data", {})
            expected_reports = int(data.get("vulns_reported", 0) or 0) if external_report_enabled else 0
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
    external_report_enabled = bool(getattr(project.project, "kk_report_enabled", True))
    prompt = render_prompt(
        load_prompt(prompt_group, "maintainer.md"),
        {
            "findings_path": findings_path,
            "project_id": proj_id,
            "group_id": retry_group_id,
            "server_url": f"http://localhost:8888",
            "mozzie_url": MOZZIE_URL,
            "mozzie_token": MOZZIE_TOKEN,
            "external_report_enabled": "true" if external_report_enabled else "false",
        },
    )

    session = driver.prepare_session()
    command = driver.build_execute(worker, prompt, session, phase="maintainer_retry")
    retry_metadata = {
        "logical_thread_id": f"intent:{proj_id}:{intent_id}",
        "attempt_id": f"maintainer-retry:{proj_id}:{intent_id}:{worker.name}:{uuid.uuid4().hex[:8]}",
        "attempt_role": "maintainer_retry",
        "intent_id": intent_id,
        "session_id": session or "",
    }
    _report_event(client, proj_id, task_type="maintainer",
                  phase="maintainer_retry", worker=worker.name, level="info",
                  message="phase started", payload=retry_metadata)

    result = run_worker_process(
        worker,
        command.argv,
        phase="maintainer_retry",
        timeout_seconds=MAINTAINER_TIMEOUT,
        project_id=proj_id,
        cancellation=cancellation,
        client=client,
        task_type="maintainer",
        event_metadata=retry_metadata,
    )

    _report_event(client, proj_id, task_type="maintainer",
                  phase="maintainer_retry", worker=worker.name,
                  level="warn" if did_timeout(result) or result.returncode not in (0, None) else "info",
                  message="phase finished", payload={
                      **retry_metadata,
                      "returncode": result.returncode,
                      "timed_out": did_timeout(result),
                      "cancelled": result.cancelled,
                  })

    if not did_timeout(result) and result.returncode == 0:
        payload = parse_json_output(result.stdout)
        if payload and payload.get("accepted"):
            data = payload.get("data", {})
            expected_reports = int(data.get("vulns_reported", 0) or 0) if external_report_enabled else 0
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
