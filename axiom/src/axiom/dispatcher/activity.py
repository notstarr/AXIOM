from __future__ import annotations

import json
import logging
import queue
import shlex
import threading
from pathlib import Path
from typing import Any, Callable

from axiom.dispatcher.heartbeat import LeaseLostError
from axiom.modules.execution.schemas import ActivityCommand
from axiom.modules.token_usage.parser import SessionParseState, parse_session_event, parse_session_file
from axiom.modules.token_usage.schemas import (
    TokenUsageBatchCommand,
    TokenUsageRecordInput,
    TokenUsageSourceInput,
)


logger = logging.getLogger(__name__)


_TOOL_PURPOSES = {
    "subfinder": "子域名枚举",
    "oneforall": "子域名枚举",
    "httpx": "服务探测与指纹识别",
    "nmap": "端口与服务扫描",
    "dirsearch": "目录枚举",
    "urlfinder": "URL 与接口发现",
    "fofa": "空间测绘查询",
    "curl": "HTTP 请求",
    "wget": "网络请求",
    "grep": "筛选运行证据",
    "find": "查找运行文件",
    "cat": "读取运行文件",
    "jq": "处理结构化数据",
    "python": "执行分析脚本",
    "python3": "执行分析脚本",
    "node": "执行分析脚本",
    "bsk": "浏览器自动化",
    "git": "读取代码仓库",
}

_SHELL_CONTROL_WORDS = {
    "do",
    "done",
    "elif",
    "else",
    "fi",
    "for",
    "function",
    "if",
    "in",
    "then",
    "while",
}

_SHELL_WRAPPERS = {"command", "env", "exec", "sudo", "timeout"}


class SessionActivityTracker:
    """Tail Pi session JSONL and report compact, lease-bound worker activity."""

    def __init__(
        self,
        session_dir: Path,
        lease_id: str,
        reporter: Callable[[ActivityCommand], None] | None,
        *,
        usage_reporter: Callable[[TokenUsageBatchCommand], None] | None = None,
        session_id: str | None = None,
        process_dir: Path | None = None,
        poll_interval_seconds: float = 0.25,
    ):
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        self.session_dir = Path(session_dir)
        self.lease_id = lease_id
        self.reporter = reporter
        self.usage_reporter = usage_reporter
        self.session_id = session_id.strip() if session_id else None
        self.process_dir = Path(process_dir) if process_dir is not None else self.session_dir.parent / "process"
        self.poll_interval_seconds = poll_interval_seconds
        self._stop = threading.Event()
        self._lease_lost = threading.Event()
        self._thread: threading.Thread | None = None
        self._positions: dict[Path, int] = {}
        self._partial: dict[Path, bytes] = {}
        self._pending: dict[str, tuple[str, str]] = {}
        self._session_states: dict[Path, SessionParseState] = {}
        self._source_errors: dict[Path, list[str]] = {}
        self._owned_paths: set[Path] = set()
        self._usage_queue: queue.Queue[TokenUsageBatchCommand | None] | None = (
            queue.Queue() if usage_reporter is not None else None
        )
        self._usage_thread: threading.Thread | None = None

    @property
    def lease_lost(self) -> bool:
        return self._lease_lost.is_set()

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("activity tracker already started")
        self.session_dir.mkdir(parents=True, exist_ok=True)
        for path in self.session_dir.glob("*.jsonl"):
            try:
                self._positions[path] = path.stat().st_size
                self._preload_session_state(path)
            except OSError:
                continue
        if self._usage_queue is not None:
            self._usage_thread = threading.Thread(
                target=self._run_usage_reports,
                name="axiom-token-usage-reporter",
                daemon=True,
            )
            self._usage_thread.start()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="axiom-session-activity",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(2.0, self.poll_interval_seconds * 4))
            self._thread = None
        if not self.lease_lost:
            self._poll_once_safely(final=True)
        if self.usage_reporter is not None:
            self._reconcile_usage_files()
            self._stop_usage_reports()

    def _run(self) -> None:
        while not self._stop.wait(self.poll_interval_seconds):
            if not self._poll_once_safely():
                return

    def _poll_once_safely(self, *, final: bool = False) -> bool:
        try:
            self._poll_once(final=final)
            return True
        except LeaseLostError:
            self._lease_lost.set()
            self._stop.set()
            return False
        except Exception:
            logger.warning("failed to report Pi session activity", exc_info=True)
            return True

    def _poll_once(self, *, final: bool = False) -> None:
        usage_records: list[TokenUsageRecordInput] = []
        for path in sorted(self.session_dir.glob("*.jsonl")):
            try:
                size = path.stat().st_size
            except OSError:
                continue
            position = self._positions.setdefault(path, 0)
            if size < position:
                position = 0
                self._partial.pop(path, None)
            if size == position:
                continue
            try:
                with path.open("rb") as handle:
                    handle.seek(position)
                    chunk = handle.read()
                    self._positions[path] = handle.tell()
            except OSError:
                continue
            payload = self._partial.pop(path, b"") + chunk
            lines = payload.split(b"\n")
            if payload and not payload.endswith(b"\n"):
                self._partial[path] = lines.pop()
            for line in lines:
                self._process_line(path, line, usage_records)
        if final:
            partial = list(self._partial.items())
            self._partial.clear()
            for path, line in partial:
                self._process_line(path, line, usage_records, final=True)
        self._flush_usage(records=usage_records)

    def _process_line(
        self,
        path: Path,
        line: bytes,
        usage_records: list[TokenUsageRecordInput],
        *,
        final: bool = False,
    ) -> None:
        if not line.strip():
            return
        try:
            event = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            if final:
                self._source_errors.setdefault(path, []).append("invalid_or_truncated_json")
            return
        if not isinstance(event, dict):
            return
        state = self._session_states.setdefault(path, SessionParseState())
        record, error = parse_session_event(
            event,
            state=state,
            session_file=f"sessions/{path.name}",
        )
        if self.session_id is None or state.session_id == self.session_id:
            self._owned_paths.add(path)
        if self.usage_reporter is not None and path in self._owned_paths:
            if record is not None:
                usage_records.append(record)
            if error:
                self._source_errors.setdefault(path, []).append(error)
        # Keep activity visible for legacy/malformed session files that do not
        # contain a session header.  Token records remain conservative and are
        # only accepted after the target session id is identified.
        if self.session_id is not None and path not in self._owned_paths and state.session_id:
            return
        if event.get("type") != "message":
            return
        message = event.get("message")
        if not isinstance(message, dict):
            return
        role = message.get("role")
        if role == "assistant":
            self._tool_calls(message)
        elif role == "toolResult":
            self._tool_result(message)

    def _preload_session_state(self, path: Path) -> None:
        if self.usage_reporter is None and self.session_id is None:
            return
        state = self._session_states.setdefault(path, SessionParseState())
        try:
            lines = path.read_bytes().split(b"\n")
        except OSError:
            return
        for raw_line in lines:
            if not raw_line.strip():
                continue
            try:
                event = json.loads(raw_line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(event, dict):
                state = self._session_states[path]
                parse_session_event(
                    event,
                    state=state,
                    session_file=f"sessions/{path.name}",
                )
        if self.session_id is None or state.session_id == self.session_id:
            self._owned_paths.add(path)

    def _reconcile_usage_files(self) -> None:
        records: list[TokenUsageRecordInput] = []
        sources: list[TokenUsageSourceInput] = []
        paths = sorted(self._owned_paths if self.session_id is not None else self.session_dir.glob("*.jsonl"))
        for path in paths:
            parsed = parse_session_file(path, session_file=f"sessions/{path.name}")
            records.extend(parsed.records)
            if parsed.source is not None:
                extra_errors = self._source_errors.get(path) or []
                if extra_errors and parsed.source.status == "complete":
                    parsed.source = parsed.source.model_copy(
                        update={
                            "status": "incomplete",
                            "error": ";".join(dict.fromkeys(extra_errors))[:1_000],
                        }
                    )
                sources.append(parsed.source)
        if not paths:
            sources.append(
                TokenUsageSourceInput(
                    session_file="sessions/missing.jsonl",
                    status="incomplete",
                    error="session_file_missing"
                    if self.session_id is None
                    else "session_for_lease_missing",
                )
            )
        self._flush_usage(records=records, sources=sources)

    def _flush_usage(
        self,
        *,
        records: list[TokenUsageRecordInput] | None = None,
        sources: list[TokenUsageSourceInput] | None = None,
    ) -> None:
        if self.usage_reporter is None:
            return
        records = records or []
        sources = sources or []
        for offset in range(0, len(records), 500):
            self._send_usage_command(
                TokenUsageBatchCommand(
                    lease_id=self.lease_id,
                    records=records[offset : offset + 500],
                )
            )
        for offset in range(0, len(sources), 100):
            self._send_usage_command(
                TokenUsageBatchCommand(
                    lease_id=self.lease_id,
                    sources=sources[offset : offset + 100],
                )
            )

    def _send_usage_command(self, command: TokenUsageBatchCommand) -> None:
        assert self.usage_reporter is not None
        if self._usage_queue is not None:
            self._usage_queue.put(command)
            return
        self._persist_usage_command(command)

    def _run_usage_reports(self) -> None:
        assert self._usage_queue is not None
        while True:
            command = self._usage_queue.get()
            try:
                if command is None:
                    return
                self._persist_usage_command(command)
            finally:
                self._usage_queue.task_done()

    def _stop_usage_reports(self) -> None:
        if self._usage_queue is None or self._usage_thread is None:
            return
        self._usage_queue.put(None)
        self._usage_thread.join(timeout=max(2.0, self.poll_interval_seconds * 4))
        self._usage_thread = None

    def _persist_usage_command(self, command: TokenUsageBatchCommand) -> None:
        assert self.usage_reporter is not None
        try:
            self.usage_reporter(command)
        except Exception:
            logger.warning("failed to persist Pi token usage", exc_info=True)
            pending_path = self.process_dir / "token-usage.pending.jsonl"
            try:
                pending_path.parent.mkdir(parents=True, exist_ok=True)
                with pending_path.open("a", encoding="utf-8") as handle:
                    handle.write(command.model_dump_json() + "\n")
            except OSError:
                logger.warning("failed to spool Pi token usage", exc_info=True)

    def _tool_calls(self, message: dict[str, Any]) -> None:
        content = message.get("content")
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "toolCall":
                continue
            call_id = str(block.get("id") or "")
            tool_name = str(block.get("name") or "unknown")
            arguments = block.get("arguments") or block.get("input") or {}
            if not isinstance(arguments, dict):
                arguments = {}
            display_tool, summary = describe_tool_call(tool_name, arguments)
            if call_id:
                self._pending[call_id] = (display_tool, summary)
            self._report(
                ActivityCommand(
                    lease_id=self.lease_id,
                    activity_kind="tool_running",
                    activity_summary=summary,
                    activity_tool=display_tool,
                )
            )

    def _tool_result(self, message: dict[str, Any]) -> None:
        call_id = str(message.get("toolCallId") or "")
        fallback = str(message.get("toolName") or "tool")
        display_tool, _summary = self._pending.pop(call_id, (fallback, ""))
        failed = bool(message.get("isError"))
        summary = (
            f"{display_tool} 执行返回错误，正在调整"
            if failed
            else f"已完成 {display_tool}，正在分析结果"
        )
        self._report(
            ActivityCommand(
                lease_id=self.lease_id,
                activity_kind="tool_result",
                activity_summary=summary,
                activity_tool=display_tool,
            )
        )

    def _report(self, command: ActivityCommand) -> None:
        if self.reporter is None:
            return
        try:
            self.reporter(command)
        except LeaseLostError:
            raise
        except Exception:
            logger.warning("activity control-plane request failed", exc_info=True)


def describe_tool_call(tool_name: str, arguments: dict[str, Any]) -> tuple[str, str]:
    normalized = tool_name.strip().lower() or "tool"
    if normalized in {"read", "write", "edit"}:
        path = arguments.get("path") or arguments.get("file_path") or ""
        filename = Path(str(path)).name if path else "文件"
        action = {"read": "读取", "write": "写入", "edit": "编辑"}[normalized]
        return normalized, f"正在{action} {filename}"
    if normalized == "bash":
        executable = shell_executable(str(arguments.get("command") or ""))
        if not executable:
            return "bash", "正在执行 Shell 命令"
        purpose = _TOOL_PURPOSES.get(executable.lower(), "命令")
        return executable, f"正在执行 {executable} {purpose}"
    return normalized, f"正在使用 {normalized}"


def shell_executable(command: str) -> str:
    """Return only an executable label; command arguments never leave the Runner."""

    fallback = ""
    for raw_line in command.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            tokens = shlex.split(line, comments=True)
        except ValueError:
            tokens = line.split()
        for raw_token in tokens:
            token = raw_token.strip(";&|(){}")
            if not token:
                continue
            if "$(" in token:
                token = token.rsplit("$(", 1)[-1].strip(";&|(){}")
            if token.startswith("$") and "axiom_bsk" in token.lower():
                token = "bsk"
            name, separator, _value = token.partition("=")
            if separator and name.replace("_", "a").isalnum() and not name[0].isdigit():
                continue
            executable = Path(token).name
            lowered = executable.lower()
            if lowered in _SHELL_CONTROL_WORDS or lowered.startswith("-"):
                continue
            if lowered in _SHELL_WRAPPERS:
                fallback = executable
                continue
            if lowered in _TOOL_PURPOSES:
                return executable
            if not fallback:
                fallback = executable
    return fallback
