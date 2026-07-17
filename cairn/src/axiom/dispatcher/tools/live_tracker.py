from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

from axiom.server.db import current_db_path

LOG = logging.getLogger(__name__)


class LiveToolTracker:
    """Polls a pi session file while the task runs, writing tool events to DB in real time.

    Tool calls and their results arrive as separate lines in the session file.
    We cache tool calls and defer writing until the matching result is available.
    """

    def __init__(
        self,
        *,
        worker_name: str,
        project_id: str,
        session_id: str = "",
        event_metadata: dict | None = None,
        phase: str = "",
        task_type: str = "",
        poll_interval: float = 0.5,
    ):
        self._worker_name = worker_name
        self._project_id = project_id
        self._session_id = session_id
        self._event_metadata = dict(event_metadata or {})
        self._phase = phase
        self._task_type = task_type
        self._poll_interval = poll_interval
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._session_path: str | None = None
        self._last_pos = 0
        # toolCall cache: id -> (tool_name, input_dict)
        self._pending_calls: dict[str, tuple[str, dict]] = {}
        # toolResult cache: id -> (output_text, is_error)
        self._pending_results: dict[str, tuple[str, bool]] = {}
        self._written_ids: set[str] = set()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._started_at = time.time()
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        # Flush any remaining pairs
        self._flush_pending()

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception:
                LOG.debug("live tool tracker tick failed", exc_info=True)
            self._stop_event.wait(self._poll_interval)

    def _tick(self) -> None:
        session_dir = Path("/tmp/axiom-pi") / self._worker_name / "sessions"
        if not session_dir.exists():
            return
        candidates = self._session_candidates(session_dir)
        if not candidates:
            return
        newest = str(candidates[0])

        # Switch to the newest file if it changed.
        if newest != self._session_path:
            self._session_path = newest
            self._last_pos = 0
            self._pending_calls.clear()
            self._pending_results.clear()
            LOG.info("live tracker watching session=%s project=%s", Path(newest).name, self._project_id)

        try:
            st = os.stat(self._session_path)
            if st.st_size < self._last_pos:
                self._last_pos = 0
            if st.st_size == self._last_pos:
                return
            with open(self._session_path, encoding="utf-8") as fh:
                fh.seek(self._last_pos)
                for line in fh:
                    self._process_line(line.strip())
                self._last_pos = fh.tell()
        except FileNotFoundError:
            self._session_path = None
        except Exception:
            LOG.debug("live tracker read error", exc_info=True)

        # Write any completed pairs
        self._flush_pending()

    def _session_candidates(self, session_dir: Path) -> list[Path]:
        files = [p for p in session_dir.iterdir() if p.suffix == ".jsonl"]
        if self._session_id:
            matches = [p for p in files if self._session_id in p.name]
            if matches:
                return sorted(matches, key=lambda p: p.stat().st_mtime, reverse=True)
        return sorted(
            [p for p in files if p.stat().st_mtime >= self._started_at],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

    def _process_line(self, line: str) -> None:
        if not line:
            return
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return
        if event.get("type") != "message":
            return
        msg = event.get("message") or {}
        role = msg.get("role", "")

        if role == "toolResult":
            call_id = msg.get("toolCallId", "")
            if not call_id or call_id in self._written_ids:
                return
            tool_name = str(msg.get("toolName", ""))
            output_text = ""
            for block in (msg.get("content") or []):
                if isinstance(block, dict) and block.get("type") == "text":
                    output_text = block.get("text", "")
                    break
            from axiom.dispatcher.tools.tracker import _is_tool_error
            is_error = _is_tool_error(
                tool_name=tool_name,
                output=output_text,
                is_error=bool(msg.get("isError", False)),
                details=msg.get("details"),
            )
            self._pending_results[call_id] = (output_text, is_error)
            return

        if role != "assistant":
            return

        for block in (msg.get("content") or []):
            if not isinstance(block, dict):
                continue
            if block.get("type") != "toolCall":
                continue
            call_id = block.get("id", "")
            if not call_id or call_id in self._written_ids:
                continue
            tool_name = block.get("name", "unknown")
            inp = block.get("arguments") or block.get("input") or {}
            self._pending_calls[call_id] = (tool_name, inp)

    def _flush_pending(self) -> None:
        """Write records for tool calls that have matching results."""
        for call_id in list(self._pending_calls):
            if call_id in self._written_ids:
                del self._pending_calls[call_id]
                continue
            if call_id not in self._pending_results:
                continue
            tool_name, inp = self._pending_calls.pop(call_id)
            output, is_error = self._pending_results.pop(call_id)
            self._write_record(call_id, tool_name, inp, output, is_error)
            self._written_ids.add(call_id)
            LOG.info("live tracker wrote tool=%s call_id=%s project=%s", tool_name, call_id[:20], self._project_id)

    def _write_record(self, call_id: str, tool_name: str, inp: dict, output: str, is_error: bool) -> None:
        db_path = current_db_path()
        if not db_path.exists():
            return
        from axiom.dispatcher.tools.tracker import _tool_input_to_argv, _truncate, _utcnow

        argv = _tool_input_to_argv(tool_name, inp)
        now = _utcnow()

        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO tool_executions (
                    id, tool, kind, argv_json, status,
                    returncode, stdout, stderr, duration_ms,
                    project_group_id, project_id, worker, task_type, phase,
                    logical_thread_id, attempt_id, intent_id, session_id,
                    created_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    call_id,
                    tool_name,
                    "builtin",
                    json.dumps(argv, ensure_ascii=False, separators=(",", ":")),
                    "error" if is_error else "success",
                    1 if is_error else 0,
                    _truncate(output) if not is_error else "",
                    _truncate(output) if is_error else "",
                    0,
                    "",
                    self._project_id,
                    self._worker_name,
                    self._task_type,
                    self._phase,
                    str(self._event_metadata.get("logical_thread_id") or ""),
                    str(self._event_metadata.get("attempt_id") or ""),
                    str(self._event_metadata.get("intent_id") or ""),
                    self._session_id or str(self._event_metadata.get("session_id") or ""),
                    now,
                    now,
                ),
            )
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
        finally:
            conn.close()
