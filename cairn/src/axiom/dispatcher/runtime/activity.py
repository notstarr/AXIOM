from __future__ import annotations

import logging
import queue
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit


LOG = logging.getLogger(__name__)


class ActivityClient(Protocol):
    def report_intent_activity(
        self,
        task_id: str,
        intent_id: str,
        worker: str,
        *,
        kind: str,
        summary: str,
        tool: str = "",
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class ActivityUpdate:
    kind: str
    summary: str
    tool: str = ""


_COMMAND_ACTIVITIES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?:^|[\s/])(nmap|fscan)(?:\s|$)"), "正在扫描{target}的开放端口和服务"),
    (re.compile(r"(?:^|[\s/])(httpx|curl|wget)(?:\s|$)"), "正在探测{target}的 Web 服务"),
    (re.compile(r"(?:^|[\s/])(subfinder|oneforall)(?:\s|$)"), "正在枚举{target}的子域名"),
    (re.compile(r"(?:^|[\s/])(dirsearch|dirsearch_bypass403)(?:\s|$)"), "正在枚举{target}的目录和接口"),
    (re.compile(r"(?:^|[\s/])(urlfinder)(?:\s|$)"), "正在分析{target}的前端资源和接口"),
    (re.compile(r"(?:^|[\s/])(fofa)(?:\s|$)"), "正在进行{target}的空间测绘查询"),
    (re.compile(r"(?:^|[\s/])(sqlmap)(?:\s|$)"), "正在验证{target}的注入风险"),
    (re.compile(r"(?:^|[\s/])(nikto|nuclei)(?:\s|$)"), "正在检查{target}的已知安全风险"),
    (re.compile(r"(?:^|[\s/])(ssh|sshpass)(?:\s|$)"), "正在验证{target}的远程登录能力"),
)


def _target_label(targets: list[str]) -> str:
    if not targets:
        return "目标"
    raw = str(targets[0]).strip()
    if not raw:
        return "目标"
    parsed = urlsplit(raw if "://" in raw else f"//{raw}")
    label = parsed.hostname or parsed.netloc or parsed.path.split("/", 1)[0] or "目标"
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port:
        label = f"{label}:{port}"
    return f" {label} "


def _event_tool(event: dict[str, Any]) -> str:
    for key in ("toolName", "tool_name", "name"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:120]
    return ""


def _event_args(event: dict[str, Any]) -> dict[str, Any]:
    for key in ("args", "arguments", "input"):
        value = event.get(key)
        if isinstance(value, dict):
            return value
    return {}


def summarize_tool_activity(tool: str, args: dict[str, Any], targets: list[str]) -> str:
    lowered_tool = tool.lower()
    command = ""
    for key in ("command", "cmd", "script"):
        value = args.get(key)
        if isinstance(value, str):
            command = value.lower()[:4096]
            break
    target = _target_label(targets)
    if lowered_tool in {"bash", "shell", "exec", "exec_command"} or command:
        for pattern, template in _COMMAND_ACTIVITIES:
            if pattern.search(command):
                return template.format(target=target).replace("  ", " ").strip()
        return f"正在对{target}执行安全测试".replace("  ", " ").strip()

    normalized = lowered_tool.replace("-", "_")
    if any(part in normalized for part in ("browser", "playwright", "navigate", "click")):
        return f"正在通过浏览器检查{target}的功能和交互".replace("  ", " ").strip()
    if any(part in normalized for part in ("read", "cat", "view")):
        return "正在读取任务资料或测试产物"
    if any(part in normalized for part in ("write", "edit", "patch", "save")):
        return "正在整理并写入测试产物"
    if any(part in normalized for part in ("grep", "search", "find", "list", "glob", "ls")):
        return "正在检索任务资料和已有证据"
    if "http" in normalized or "request" in normalized:
        return f"正在探测{target}的 Web 服务".replace("  ", " ").strip()
    return f"正在使用 {tool or '工具'} 执行测试"


class IntentActivityReporter:
    """Translate PI RPC events into low-volume, sanitized Intent activity updates."""

    def __init__(
        self,
        client: ActivityClient,
        *,
        task_id: str,
        intent_id: str,
        worker: str,
        targets: list[str],
        min_interval_seconds: float = 1.0,
    ) -> None:
        self.client = client
        self.task_id = task_id
        self.intent_id = intent_id
        self.worker = worker
        self.targets = targets
        self.min_interval_seconds = min_interval_seconds
        self._queue: queue.Queue[ActivityUpdate | None] = queue.Queue(maxsize=32)
        self._last_update: ActivityUpdate | None = None
        self._last_queued_at = 0.0
        self._tools: dict[str, tuple[str, dict[str, Any]]] = {}
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def handle_event(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("type") or "")
        if event_type == "agent_start":
            self.report("starting", "Worker 正在读取任务上下文")
            return
        if event_type == "tool_execution_start":
            tool = _event_tool(event)
            args = _event_args(event)
            call_id = str(event.get("toolCallId") or event.get("tool_call_id") or "")
            if call_id:
                self._tools[call_id] = (tool, args)
            self.report(
                "tool_running",
                summarize_tool_activity(tool, args, self.targets),
                tool=tool,
            )
            return
        if event_type == "tool_execution_update":
            call_id = str(event.get("toolCallId") or event.get("tool_call_id") or "")
            tool, args = self._tools.get(call_id, (_event_tool(event), _event_args(event)))
            if tool:
                summary = summarize_tool_activity(tool, args, self.targets)
                self.report("tool_waiting", f"{summary}，正在等待工具返回", tool=tool)
            return
        if event_type == "tool_execution_end":
            call_id = str(event.get("toolCallId") or event.get("tool_call_id") or "")
            tool, _args = self._tools.pop(call_id, (_event_tool(event), {}))
            self.report("analyzing", "工具执行完成，正在分析返回结果", tool=tool)
            return
        if event_type == "agent_end":
            self.report("organizing", "正在检查并整理测试产物")

    def report(self, kind: str, summary: str, *, tool: str = "", force: bool = False) -> None:
        update = ActivityUpdate(kind=kind, summary=summary, tool=tool)
        now = time.monotonic()
        if not force and self._last_update == update:
            return
        if (
            not force
            and self._last_update is not None
            and self._last_update.kind == update.kind
            and now - self._last_queued_at < self.min_interval_seconds
        ):
            return
        self._last_update = update
        self._last_queued_at = now
        try:
            self._queue.put_nowait(update)
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(update)
            except queue.Full:
                pass

    def close(self) -> None:
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self._thread.join(timeout=1)

    def _run(self) -> None:
        while True:
            update = self._queue.get()
            if update is None:
                return
            try:
                self.client.report_intent_activity(
                    self.task_id,
                    self.intent_id,
                    self.worker,
                    kind=update.kind,
                    summary=update.summary,
                    tool=update.tool,
                )
            except Exception:
                # Activity reporting is observational. It must never fail or
                # delay the actual security-testing run.
                LOG.debug(
                    "failed to report Intent activity task=%s intent=%s",
                    self.task_id,
                    self.intent_id,
                    exc_info=True,
                )
