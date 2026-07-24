from __future__ import annotations

import json
import time
from pathlib import Path

from axiom.dispatcher.activity import SessionActivityTracker, describe_tool_call, shell_executable


def _append(path: Path, payload: dict) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _wait_for(items: list, size: int) -> None:
    deadline = time.monotonic() + 2
    while len(items) < size and time.monotonic() < deadline:
        time.sleep(0.02)
    assert len(items) >= size


def _message(role: str, content: list[dict], **extra) -> dict:
    return {"type": "message", "message": {"role": role, "content": content, **extra}}


def test_tracker_reports_tool_call_before_result_and_ignores_existing_session(tmp_path) -> None:
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    session = session_dir / "session.jsonl"
    _append(
        session,
        _message(
            "assistant",
            [{"type": "toolCall", "id": "old", "name": "bash", "arguments": {"command": "nmap host"}}],
        ),
    )
    events = []
    tracker = SessionActivityTracker(session_dir, "lease-1", events.append, poll_interval_seconds=0.02)
    tracker.start()
    time.sleep(0.08)
    assert events == []

    _append(
        session,
        _message(
            "assistant",
            [{"type": "toolCall", "id": "new", "name": "bash", "arguments": {"command": "subfinder -d example.com"}}],
        ),
    )
    _wait_for(events, 1)
    assert events[0].activity_tool == "subfinder"
    assert events[0].activity_kind == "tool_running"
    assert "子域名枚举" in events[0].activity_summary

    _append(
        session,
        _message(
            "toolResult",
            [{"type": "text", "text": "done"}],
            toolCallId="new",
            toolName="bash",
        ),
    )
    _wait_for(events, 2)
    assert events[1].activity_kind == "tool_result"
    assert events[1].activity_tool == "subfinder"
    tracker.stop()


def test_tracker_handles_partial_json_lines_and_stops(tmp_path) -> None:
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    session = session_dir / "session.jsonl"
    events = []
    tracker = SessionActivityTracker(session_dir, "lease-1", events.append, poll_interval_seconds=0.02)
    tracker.start()
    payload = json.dumps(
        _message("assistant", [{"type": "toolCall", "id": "1", "name": "read", "arguments": {"path": "SKILL.md"}}]),
        ensure_ascii=False,
    )
    with session.open("a", encoding="utf-8") as handle:
        handle.write(payload[:20])
        handle.flush()
        time.sleep(0.08)
        assert events == []
        handle.write(payload[20:] + "\n")
    _wait_for(events, 1)
    assert events[0].activity_summary == "正在读取 SKILL.md"
    tracker.stop()
    assert tracker._thread is None


def test_tracker_flushes_a_final_json_line_without_newline(tmp_path) -> None:
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    session = session_dir / "session.jsonl"
    events = []
    tracker = SessionActivityTracker(session_dir, "lease-1", events.append, poll_interval_seconds=1)
    tracker.start()
    session.write_text(
        json.dumps(
            _message("assistant", [{"type": "toolCall", "id": "1", "name": "write", "arguments": {"path": "findings.md"}}]),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    tracker.stop()
    assert events[0].activity_summary == "正在写入 findings.md"


def test_activity_labels_never_include_the_shell_command() -> None:
    command = "TOKEN=secret-value\nsubfinder -d example.com -o results.txt"
    assert shell_executable(command) == "subfinder"
    _tool, summary = describe_tool_call("bash", {"command": command})
    assert summary == "正在执行 subfinder 子域名枚举"
    assert "secret-value" not in summary
