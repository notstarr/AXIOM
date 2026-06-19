from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from axiom.dispatcher.tools.live_tracker import LiveToolTracker
from axiom.dispatcher.tools.tracker import record_tool_executions_from_session

from axiom.dispatcher.config import DispatchConfig, WorkerConfig
from axiom.dispatcher.proxy import proxy_env_for_phase
from axiom.dispatcher.protocol.client import CairnClient
from axiom.dispatcher.runtime.cancellation import TaskCancellation
from axiom.dispatcher.runtime.heartbeat import HeartbeatLease
from axiom.dispatcher.runtime.process import ManagedProcess, ProcessResult
from axiom.dispatcher.tools.gateway import default_gateway

HEALTHCHECK_COMMUNICATE_GRACE_SECONDS = 10
PROCESS_COMMUNICATE_GRACE_SECONDS = 15
LOG_PREVIEW_LIMIT = 8000
GRAPH_SNAPSHOT_ROOT = "/tmp/axiom-prompts"
LOG = logging.getLogger(__name__)
_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"(api[_-]?key\s*[=:]\s*)([^\s\"']+)", re.IGNORECASE),
    re.compile(r"(authorization\s*[:=]\s*bearer\s+)([^\s\"']+)", re.IGNORECASE),
)


@dataclass(slots=True)
class HealthcheckRun:
    result: ProcessResult
    duration_ms: int


@dataclass(slots=True)
class ConcludeWriteResult:
    status: str
    fact_id: str | None = None


def preview(text: str, limit: int = LOG_PREVIEW_LIMIT) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[:limit] + "..."


def did_timeout(result: ProcessResult) -> bool:
    return not result.cancelled and (result.timed_out or result.returncode in (124, 137))


def cancel_reason(result: ProcessResult, cancellation: TaskCancellation | None = None) -> str | None:
    if result.cancelled:
        return result.cancel_reason or "cancelled"
    if cancellation is not None:
        return cancellation.reason
    return None


def communicate_timeout(timeout_seconds: int, grace_seconds: int = PROCESS_COMMUNICATE_GRACE_SECONDS) -> int:
    return timeout_seconds + grace_seconds


def task_healthcheck_enabled(config: DispatchConfig) -> bool:
    return config.runtime.worker_healthcheck == "startup_and_task"


def write_graph_snapshot_file(project_id: str, graph_yaml: str, *, phase: str) -> str:
    """Write graph YAML to a temp file and return the path for prompt reference."""
    dir_path = Path(GRAPH_SNAPSHOT_ROOT) / f"{phase}-{uuid.uuid4().hex[:12]}"
    dir_path.mkdir(parents=True, exist_ok=True)
    file_path = dir_path / "graph.yaml"
    file_path.write_text(graph_yaml, encoding="utf-8")
    return (
        "The graph YAML snapshot is stored in this file inside the current container:\n\n"
        f"{file_path}\n\n"
        "Before using the graph, read the entire file and treat its contents as the YAML snapshot "
        "for this Graph section."
    )


def build_process(
    env: dict[str, str],
    argv: list[str],
    *,
    timeout_seconds: int,
    cwd: str | None = None,
) -> ManagedProcess:
    """Build a ManagedProcess directly (no container abstraction)."""
    return ManagedProcess(argv, env, cwd=cwd)


def run_healthcheck(
    worker: WorkerConfig,
    command: list[str],
    *,
    timeout_seconds: int,
    project_id: str,
    lease: HeartbeatLease | None = None,
    cancellation: TaskCancellation | None = None,
    client: CairnClient | None = None,
    task_type: str = "system",
) -> HealthcheckRun:
    gateway = default_gateway()
    invocation = gateway.prepare("healthcheck", command, project_id=project_id)
    if client is not None and project_id is not None:
        _report_event(
            client,
            project_id,
            task_type=task_type,
            phase="healthcheck",
            worker=worker.name,
            level="info",
            message="healthcheck started",
            payload={
                "timeout_seconds": timeout_seconds,
                "tool": invocation.tool,
                "tool_kind": invocation.kind,
            },
        )
    env = _worker_env_with_runtime_keys(worker, phase="healthcheck")
    process = ManagedProcess(invocation.argv, env)
    process.start()
    if lease is not None:
        lease.attach_process(process)
    if cancellation is not None:
        cancellation.attach_process(process)
    started = time.perf_counter()
    try:
        result = process.communicate(timeout=communicate_timeout(timeout_seconds, HEALTHCHECK_COMMUNICATE_GRACE_SECONDS))
    finally:
        if lease is not None:
            lease.attach_process(None)
        if cancellation is not None:
            cancellation.attach_process(None)
    duration_ms = int((time.perf_counter() - started) * 1000)
    if client is not None and project_id is not None:
        _report_event(
            client,
            project_id,
            task_type=task_type,
            phase="healthcheck",
            worker=worker.name,
            level="info" if result.returncode == 0 else "warn",
            message="healthcheck finished",
            payload={
                "returncode": result.returncode,
                "duration_ms": duration_ms,
                "stderr_preview": _sanitize_text(preview(result.stderr)),
            },
        )
    return HealthcheckRun(result=result, duration_ms=duration_ms)


def run_worker_process(
    worker: WorkerConfig,
    argv: list[str],
    *,
    phase: str,
    timeout_seconds: int,
    project_id: str,
    lease: HeartbeatLease | None = None,
    cancellation: TaskCancellation | None = None,
    client: CairnClient | None = None,
    task_type: str = "system",
) -> ProcessResult:
    gateway = default_gateway()
    try:
        invocation = gateway.prepare(phase, argv, project_id=project_id)
    except ValueError as exc:
        if client is not None and project_id is not None:
            _report_event(
                client,
                project_id,
                task_type=task_type,
                phase=phase,
                worker=worker.name,
                level="warn",
                message="phase blocked by policy",
                payload={"reason": str(exc), "command_preview": _sanitize_command(argv)},
            )
        return ProcessResult(returncode=1, stdout="", stderr=str(exc), timed_out=False, cancelled=False)
    LOG.info(
        "starting local exec worker=%s phase=%s timeout=%ss",
        worker.name,
        phase,
        timeout_seconds,
    )
    if client is not None and project_id is not None:
        _report_event(
            client,
            project_id,
            task_type=task_type,
            phase=phase,
            worker=worker.name,
            level="info",
            message="phase started",
            payload={
                "timeout_seconds": timeout_seconds,
                "tool": invocation.tool,
                "tool_kind": invocation.kind,
                "command_preview": _sanitize_command(invocation.argv),
            },
        )
    env = _worker_env_with_runtime_keys(worker, phase=phase)
    process = ManagedProcess(invocation.argv, env)
    process.start()
    # Start live tool tracking — polls the session file while pi runs.
    live_tracker = None  # type: LiveToolTracker | None
    if project_id:
        live_tracker = LiveToolTracker(
            worker_name=worker.name,
            project_id=project_id,
        )
    if live_tracker:
        live_tracker.start()
    if lease is not None:
        lease.attach_process(process)
    if cancellation is not None:
        cancellation.attach_process(process)
    try:
        result = process.communicate(timeout=communicate_timeout(timeout_seconds))
        if client is not None and project_id is not None:
            level = "info" if result.returncode == 0 and not result.timed_out else "warn"
            token_usage = _extract_token_usage(result.stdout)
            payload = {
                "tool": invocation.tool,
                "tool_kind": invocation.kind,
                "command_preview": _sanitize_command(invocation.argv),
                "returncode": result.returncode,
                "timed_out": result.timed_out,
                "cancelled": result.cancelled,
                "cancel_reason": result.cancel_reason,
                "stdout_preview": _sanitize_text(preview(result.stdout)),
                "stderr_preview": _sanitize_text(preview(result.stderr)),
            }
            if token_usage is not None:
                payload["token_usage"] = token_usage
            _report_event(
                client,
                project_id,
                task_type=task_type,
                phase=phase,
                worker=worker.name,
                level=level,
                message="phase finished",
                payload=payload,
            )
        return result
    finally:
        try:
            if live_tracker:
                live_tracker.stop()
        except NameError:
            pass
        if lease is not None:
            lease.attach_process(None)
        if cancellation is not None:
            cancellation.attach_process(None)


def _worker_env_with_runtime_keys(worker: WorkerConfig, *, phase: str) -> dict[str, str]:
    """Build the process environment for a worker, injecting proxy settings."""
    env = dict(worker.env)
    # Inject the AXIOM server URL so pi skills can call the API via curl.
    env.setdefault("AXIOM_SERVER", "http://127.0.0.1:8000")
    api_base_url = env.get("PI_BASE_URL", "")
    env.update(proxy_env_for_phase(phase, api_base_url=api_base_url))
    # Drop placeholder entries so that pi falls back to models.json's apiKey.
    pi_key = env.get("PI_API_KEY", "")
    if not pi_key or pi_key == "<stored-in-database>":
        env.pop("PI_API_KEY", None)
    return env


def _report_event(
    client: CairnClient,
    project_id: str,
    *,
    task_type: str,
    phase: str,
    worker: str,
    level: str,
    message: str,
    payload: dict[str, Any],
) -> None:
    response = client.report_event(
        project_id,
        task_type=task_type,
        phase=phase,
        worker=worker,
        level=level,
        message=message,
        payload=payload,
    )
    if not response.ok and response.status_code not in (403, 404):
        LOG.debug("task event report failed project=%s task=%s phase=%s status=%s", project_id, task_type, phase, response.status_code)


def report_task_event(
    client: CairnClient,
    project_id: str,
    *,
    task_type: str,
    phase: str,
    worker: str,
    level: str,
    message: str,
    payload: dict[str, Any],
) -> None:
    _report_event(
        client,
        project_id,
        task_type=task_type,
        phase=phase,
        worker=worker,
        level=level,
        message=message,
        payload=payload,
    )


def _sanitize_command(argv: list[str]) -> str:
    return _sanitize_text(" ".join(argv))


def _sanitize_text(text: str) -> str:
    value = text
    for pattern in _SECRET_PATTERNS:
        if pattern.groups == 0:
            value = pattern.sub("***", value)
        else:
            value = pattern.sub(r"\1***", value)
    return value


def _extract_token_usage(stdout: str) -> dict[str, int] | None:
    if not stdout.strip():
        return None
    candidates: list[dict[str, int]] = []
    for line in stdout.splitlines():
        raw = line.strip()
        if not raw or not (raw.startswith("{") and raw.endswith("}")):
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for usage in _find_usage_candidates(payload):
            if usage is not None:
                candidates.append(usage)
    if not candidates:
        return None
    return max(candidates, key=lambda item: item["total_tokens"])


def _find_usage_candidates(value: Any) -> list[dict[str, int] | None]:
    hits: list[dict[str, int] | None] = []
    if isinstance(value, dict):
        parsed = _normalize_usage_map(value)
        if parsed is not None:
            hits.append(parsed)
        for nested in value.values():
            hits.extend(_find_usage_candidates(nested))
    elif isinstance(value, list):
        for item in value:
            hits.extend(_find_usage_candidates(item))
    return hits


def _normalize_usage_map(payload: dict[str, Any]) -> dict[str, int] | None:
    # pi uses "input"/"output"/"totalTokens"; OpenAI uses "input_tokens"/"output_tokens"
    input_tokens = _to_non_negative_int(
        payload.get("input_tokens",
        payload.get("prompt_tokens",
        payload.get("inputTokens",
        payload.get("input"))))  # pi native format
    )
    output_tokens = _to_non_negative_int(
        payload.get("output_tokens",
        payload.get("completion_tokens",
        payload.get("outputTokens",
        payload.get("output"))))  # pi native format
    )
    total_tokens = _to_non_negative_int(payload.get("total_tokens", payload.get("totalTokens")))
    if input_tokens is None and output_tokens is None and total_tokens is None:
        return None
    if input_tokens is None:
        input_tokens = 0
    if output_tokens is None:
        output_tokens = 0
    if total_tokens is None:
        total_tokens = input_tokens + output_tokens
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }


def _to_non_negative_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        iv = int(value)
        return iv if iv >= 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        iv = int(value.strip())
        return iv if iv >= 0 else None
    return None


def project_allows_conclude_fallback(client: CairnClient, project_id: str, *, worker_name: str, intent_id: str) -> bool:
    project = client.get_project(project_id)
    if project.project.status == "active":
        return True
    LOG.info(
        "skip conclude fallback because project is no longer active project=%s intent=%s worker=%s status=%s",
        project_id,
        intent_id,
        worker_name,
        project.project.status,
    )
    return False


def best_effort_release_reason(client: CairnClient, project_id: str, worker_name: str) -> None:
    response = client.release_reason(project_id, worker_name)
    if not response.ok and response.status_code not in (403, 409):
        LOG.warning(
            "reason release failed project=%s worker=%s status=%s",
            project_id,
            worker_name,
            response.status_code,
        )
    elif response.ok:
        LOG.info("released reason project=%s worker=%s", project_id, worker_name)
    else:
        LOG.info(
            "reason release skipped project=%s worker=%s status=%s",
            project_id,
            worker_name,
            response.status_code,
        )


def _format_blackboard(items: list, group_id: str = "") -> str:
    """Inject blackboard as a compact index. AI fetches full content on demand via bash curl.

    Only title + hint are injected. The agent reads the index and calls bash to
    fetch any item's full content when it decides it's relevant, avoiding loading
    every item's potentially long content into the context window upfront.
    """
    if not items:
        return "（无）"

    lines: list[str] = []
    for item in items:
        if hasattr(item, "title"):
            item_id = (item.id or "").strip()
            title = (item.title or "").strip()
            hint = (getattr(item, "hint", "") or "").strip()
        else:
            item_id = (item.get("id") or "").strip()
            title = (item.get("title") or "").strip()
            hint = (item.get("hint") or "").strip()

        label = title or item_id
        if not label:
            continue
        hint_part = f"  ← {hint}" if hint else ""
        lines.append(f"  [{item_id}] {label}{hint_part}")

    if not lines:
        return "（无）"

    if group_id:
        fetch_cmd = f"curl -s $AXIOM_SERVER/project-groups/{group_id}/blackboard/<item_id>"
    else:
        fetch_cmd = "curl -s $AXIOM_SERVER/project-groups/<group_id>/blackboard/<item_id>"

    header = (
        "以下是黑板条目索引（仅标题和使用提示）。"
        "如需读取某条完整内容，用 bash 工具执行：\n"
        f"  {fetch_cmd}\n"
        "条目列表："
    )
    return header + "\n" + "\n".join(lines)


def write_conclude_result(
    client: CairnClient,
    project_id: str,
    intent_id: str,
    worker_name: str,
    description: str,
    *,
    source: str,
    phase_ms: int,
    total_ms: int | None = None,
) -> str:
    return write_conclude_result_with_fact_id(
        client,
        project_id,
        intent_id,
        worker_name,
        description,
        source=source,
        phase_ms=phase_ms,
        total_ms=total_ms,
    ).status


def write_conclude_result_with_fact_id(
    client: CairnClient,
    project_id: str,
    intent_id: str,
    worker_name: str,
    description: str,
    *,
    source: str,
    phase_ms: int,
    total_ms: int | None = None,
) -> ConcludeWriteResult:
    response = client.conclude(project_id, intent_id, worker_name, description)
    if response.ok:
        fact_id: str | None = None
        if isinstance(response.data, dict):
            fact = response.data.get("fact")
            if isinstance(fact, dict):
                candidate = fact.get("id")
                if isinstance(candidate, str) and candidate:
                    fact_id = candidate
        if total_ms is None:
            LOG.info(
                "intent concluded project=%s intent=%s worker=%s source=%s phase_ms=%s",
                project_id,
                intent_id,
                worker_name,
                source,
                phase_ms,
            )
        else:
            LOG.info(
                "intent concluded project=%s intent=%s worker=%s source=%s phase_ms=%s total_ms=%s",
                project_id,
                intent_id,
                worker_name,
                source,
                phase_ms,
                total_ms,
            )
        return ConcludeWriteResult(status="success", fact_id=fact_id)
    if response.status_code == 403:
        LOG.info(
            "project became inactive during conclude project=%s intent=%s worker=%s",
            project_id,
            intent_id,
            worker_name,
        )
    else:
        LOG.warning(
            "conclude write failed project=%s intent=%s worker=%s status=%s body=%s",
            project_id,
            intent_id,
            worker_name,
            response.status_code,
            response.text,
        )
    best_effort_release(client, project_id, intent_id, worker_name)
    return ConcludeWriteResult(status="failed", fact_id=None)


def best_effort_release(client: CairnClient, project_id: str, intent_id: str, worker_name: str) -> None:
    response = client.release(project_id, intent_id, worker_name)
    if not response.ok and response.status_code not in (403, 409):
        LOG.warning(
            "release failed project=%s intent=%s worker=%s status=%s",
            project_id,
            intent_id,
            worker_name,
            response.status_code,
        )
    elif response.ok:
        LOG.info("released intent project=%s intent=%s worker=%s", project_id, intent_id, worker_name)
    else:
        LOG.info(
            "release skipped project=%s intent=%s worker=%s status=%s",
            project_id,
            intent_id,
            worker_name,
            response.status_code,
        )


def record_session_tools(
    *,
    session_id: str,
    worker_name: str,
    project_id: str,
    project_group_id: str | None = None,
) -> tuple[int, dict[str, int]]:
    """Find the session file for a pi session and persist tool executions.

    Returns (records_inserted, token_usage) where token_usage has
    input_tokens, output_tokens, total_tokens summed from the session.
    """
    session_dir = Path("/tmp/axiom-pi") / worker_name / "sessions"
    if not session_dir.exists():
        return 0, {}
    # Try matching by session_id first.
    if session_id:
        for candidate in sorted(session_dir.iterdir(), reverse=True):
            if not candidate.name.endswith(".jsonl"):
                continue
            if session_id in candidate.name:
                return record_tool_executions_from_session(
                    session_path=str(candidate),
                    project_id=project_id,
                    project_group_id=project_group_id,
                )
    # Fallback: use the most recently modified session file.
    candidates = sorted(
        [p for p in session_dir.iterdir() if p.suffix == ".jsonl"],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if candidates:
        return record_tool_executions_from_session(
            session_path=str(candidates[0]),
            project_id=project_id,
            project_group_id=project_group_id,
        )
    LOG.debug("no session files found for worker=%s", worker_name)
    return 0, {}
