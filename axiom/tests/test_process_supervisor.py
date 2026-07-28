from __future__ import annotations

import json
import sys
import threading
import time

from axiom.dispatcher.process import (
    ProcessSpec,
    ProcessSupervisor,
    RpcProcessSpec,
    RpcProcessSupervisor,
)


def test_process_supervisor_preserves_logs_and_returns_success(tmp_path) -> None:
    result = ProcessSupervisor().run(
        ProcessSpec(
            command=["/bin/sh", "-c", "printf out; printf err >&2"],
            log_dir=tmp_path / "logs",
            timeout_seconds=2,
        )
    )

    assert result.returncode == 0
    assert result.stdout == "out"
    assert result.stderr == "err"
    assert result.stdout_path.read_text() == "out"
    assert result.stderr_path.read_text() == "err"


def test_process_supervisor_terminates_a_process_group_on_timeout(tmp_path) -> None:
    started = time.monotonic()
    result = ProcessSupervisor().run(
        ProcessSpec(
            command=["/bin/sh", "-c", "sleep 30"],
            log_dir=tmp_path / "logs",
            timeout_seconds=0.1,
            terminate_grace_seconds=0.2,
            poll_interval_seconds=0.02,
        )
    )

    assert result.timed_out is True
    assert result.returncode != 0
    assert time.monotonic() - started < 3


def test_process_supervisor_cancels_when_lease_or_task_is_lost(tmp_path) -> None:
    cancelled = threading.Event()

    def should_cancel() -> str | None:
        return "task_stopped" if cancelled.is_set() else None

    def cancel_later() -> None:
        time.sleep(0.1)
        cancelled.set()

    thread = threading.Thread(target=cancel_later)
    thread.start()
    result = ProcessSupervisor().run(
        ProcessSpec(
            command=["/bin/sh", "-c", "sleep 30"],
            log_dir=tmp_path / "logs",
            timeout_seconds=5,
            terminate_grace_seconds=0.2,
            poll_interval_seconds=0.02,
        ),
        should_cancel=should_cancel,
    )
    thread.join()

    assert result.cancelled is True
    assert result.cancel_reason == "task_stopped"
    assert result.timed_out is False


def test_rpc_process_supervisor_prompts_steers_once_and_reads_finish(tmp_path) -> None:
    command_log = tmp_path / "commands.jsonl"
    script = _rpc_script(
        tmp_path,
        """
prompt = read_command()
record(prompt)
emit({"id": prompt["id"], "type": "response", "command": "prompt", "success": True})
emit({"type": "tool_execution_start", "toolCallId": "call-1"})
time.sleep(0.12)
steer = read_command()
record(steer)
emit({"id": steer["id"], "type": "response", "command": "steer", "success": True})
emit({"type": "tool_execution_end", "toolCallId": "call-1"})
emit({
    "type": "agent_end",
    "messages": [{"role": "assistant", "content": [{"type": "text", "text": "FINISH"}]}],
})
settle("FINISH")
""",
    )
    reminders: list[str] = []

    result = RpcProcessSupervisor().run(
        RpcProcessSpec(
            command=[sys.executable, str(script)],
            log_dir=tmp_path / "logs",
            timeout_seconds=1,
            poll_interval_seconds=0.01,
            environment={"COMMAND_LOG": str(command_log)},
            prompt="执行测试",
            steer_after_seconds=0.05,
            steer_message="立即收口",
        ),
        on_steer=lambda: reminders.append("sent"),
    )

    commands = [json.loads(line) for line in command_log.read_text().splitlines()]
    assert [command["type"] for command in commands] == [
        "prompt",
        "steer",
        "get_last_assistant_text",
    ]
    assert commands[0]["message"] == "执行测试"
    assert commands[1]["message"] == "立即收口"
    assert reminders == ["sent"]
    assert result.returncode == 0
    assert result.timed_out is False
    assert result.last_assistant_text == "FINISH"


def test_rpc_process_supervisor_does_not_steer_an_early_completion(tmp_path) -> None:
    command_log = tmp_path / "commands.jsonl"
    script = _rpc_script(
        tmp_path,
        """
prompt = read_command()
record(prompt)
emit({"id": prompt["id"], "type": "response", "command": "prompt", "success": True})
emit({
    "type": "agent_end",
    "messages": [{"role": "assistant", "content": [{"type": "text", "text": "FINISH"}]}],
})
settle("FINISH")
""",
    )
    reminders: list[str] = []

    result = RpcProcessSupervisor().run(
        RpcProcessSpec(
            command=[sys.executable, str(script)],
            log_dir=tmp_path / "logs",
            timeout_seconds=1,
            poll_interval_seconds=0.01,
            environment={"COMMAND_LOG": str(command_log)},
            prompt="执行测试",
            steer_after_seconds=0.5,
            steer_message="立即收口",
        ),
        on_steer=lambda: reminders.append("sent"),
    )

    commands = [json.loads(line) for line in command_log.read_text().splitlines()]
    assert [command["type"] for command in commands] == [
        "prompt",
        "get_last_assistant_text",
    ]
    assert reminders == []
    assert result.returncode == 0
    assert result.last_assistant_text == "FINISH"


def test_rpc_process_supervisor_waits_for_retry_and_agent_settled(tmp_path) -> None:
    command_log = tmp_path / "commands.jsonl"
    script = _rpc_script(
        tmp_path,
        """
prompt = read_command()
record(prompt)
emit({"id": prompt["id"], "type": "response", "command": "prompt", "success": True})
emit({
    "type": "agent_end",
    "willRetry": True,
    "messages": [{
        "role": "assistant",
        "content": [{"type": "text", "text": "STALE"}],
        "stopReason": "error",
    }],
})
emit({"type": "auto_retry_start", "attempt": 1, "maxAttempts": 3, "delayMs": 0})
time.sleep(0.05)
emit({
    "type": "agent_end",
    "willRetry": False,
    "messages": [{
        "role": "assistant",
        "content": [{"type": "text", "text": "FINISH"}],
        "stopReason": "stop",
    }],
})
emit({"type": "auto_retry_end", "success": True, "attempt": 1})
settle("FINISH")
""",
    )

    result = RpcProcessSupervisor().run(
        RpcProcessSpec(
            command=[sys.executable, str(script)],
            log_dir=tmp_path / "logs",
            timeout_seconds=1,
            poll_interval_seconds=0.01,
            environment={"COMMAND_LOG": str(command_log)},
            prompt="执行测试",
        )
    )

    commands = [json.loads(line) for line in command_log.read_text().splitlines()]
    assert [command["type"] for command in commands] == [
        "prompt",
        "get_last_assistant_text",
    ]
    assert result.returncode == 0
    assert result.last_assistant_text == "FINISH"
    assert "STALE" in result.stdout


def test_rpc_process_supervisor_returns_terminal_provider_failure_after_settle(
    tmp_path,
) -> None:
    command_log = tmp_path / "commands.jsonl"
    script = _rpc_script(
        tmp_path,
        """
prompt = read_command()
record(prompt)
emit({"id": prompt["id"], "type": "response", "command": "prompt", "success": True})
emit({
    "type": "agent_end",
    "willRetry": False,
    "messages": [{
        "role": "assistant",
        "content": [],
        "stopReason": "error",
        "errorMessage": "HTTP 429: quota exhausted",
    }],
})
emit({
    "type": "auto_retry_end",
    "success": False,
    "attempt": 3,
    "finalError": "HTTP 429: quota exhausted",
})
emit({"type": "agent_settled"})
abort = read_command()
record(abort)
""",
    )

    result = RpcProcessSupervisor().run(
        RpcProcessSpec(
            command=[sys.executable, str(script)],
            log_dir=tmp_path / "logs",
            timeout_seconds=5,
            poll_interval_seconds=0.01,
            environment={"COMMAND_LOG": str(command_log)},
            prompt="执行测试",
        )
    )

    commands = [json.loads(line) for line in command_log.read_text().splitlines()]
    assert [command["type"] for command in commands] == ["prompt", "abort"]
    assert result.returncode != 0
    assert result.timed_out is False
    assert result.last_assistant_text is None
    assert "HTTP 429: quota exhausted" in result.stderr


def test_rpc_process_supervisor_bounds_missing_settle_after_provider_failure(
    tmp_path,
) -> None:
    command_log = tmp_path / "commands.jsonl"
    script = _rpc_script(
        tmp_path,
        """
prompt = read_command()
record(prompt)
emit({"id": prompt["id"], "type": "response", "command": "prompt", "success": True})
emit({
    "type": "agent_end",
    "willRetry": False,
    "messages": [{
        "role": "assistant",
        "content": [],
        "stopReason": "error",
        "errorMessage": "HTTP 503: unavailable",
    }],
})
emit({
    "type": "auto_retry_end",
    "success": False,
    "attempt": 3,
    "finalError": "HTTP 503: unavailable",
})
abort = read_command()
record(abort)
""",
    )
    started = time.monotonic()

    result = RpcProcessSupervisor().run(
        RpcProcessSpec(
            command=[sys.executable, str(script)],
            log_dir=tmp_path / "logs",
            timeout_seconds=5,
            terminate_grace_seconds=0.5,
            poll_interval_seconds=0.01,
            provider_failure_grace_seconds=0.05,
            environment={"COMMAND_LOG": str(command_log)},
            prompt="执行测试",
        )
    )

    commands = [json.loads(line) for line in command_log.read_text().splitlines()]
    assert [command["type"] for command in commands] == ["prompt", "abort"]
    assert result.returncode != 0
    assert result.timed_out is False
    assert "did not settle within 0.05s" in result.stderr
    assert "HTTP 503: unavailable" in result.stderr
    assert time.monotonic() - started < 1


def test_rpc_process_supervisor_omits_cumulative_stream_events_from_log(tmp_path) -> None:
    script = _rpc_script(
        tmp_path,
        """
prompt = read_command()
emit({"id": prompt["id"], "type": "response", "command": "prompt", "success": True})
partial = ""
for index in range(2000):
    partial += "x"
    emit({
        "type": "message_update",
        "assistantMessageEvent": {
            "type": "text_delta",
            "contentIndex": 0,
            "delta": "x",
            "partial": {"role": "assistant", "content": [{"type": "text", "text": partial}]},
        },
        "message": {"role": "assistant", "content": [{"type": "text", "text": partial}]},
    })
for index in range(25):
    emit({
        "type": "tool_execution_update",
        "toolCallId": "call-1",
        "toolName": "bash",
        "args": {"command": "printf x"},
        "partialResult": {"content": [{"type": "text", "text": "x" * (index + 1)}]},
    })
for index in range(10):
    emit({"type": "bash_execution_update", "id": "bash-1", "output": "x" * (index + 1)})
emit({"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": "FINISH"}]}})
settle("FINISH")
""",
    )

    result = RpcProcessSupervisor().run(
        RpcProcessSpec(
            command=[sys.executable, str(script)],
            log_dir=tmp_path / "logs",
            timeout_seconds=5,
            poll_interval_seconds=0.01,
            prompt="执行测试",
        )
    )

    assert result.returncode == 0
    assert result.last_assistant_text == "FINISH"
    assert result.stdout_path.stat().st_size < 16 * 1024
    logged = [json.loads(line) for line in result.stdout_path.read_text().splitlines()]
    assert not any(
        event.get("type")
        in {"message_update", "tool_execution_update", "bash_execution_update"}
        for event in logged
    )
    assert logged[-1] == {
        "type": "axiom_rpc_log_summary",
        "omitted_events": {
            "bash_execution_update": 10,
            "message_update": 2000,
            "tool_execution_update": 25,
        },
    }


def test_rpc_process_supervisor_rejects_eof_before_agent_settled(tmp_path) -> None:
    script = _rpc_script(
        tmp_path,
        """
prompt = read_command()
emit({"id": prompt["id"], "type": "response", "command": "prompt", "success": True})
emit({
    "type": "agent_end",
    "willRetry": False,
    "messages": [{"role": "assistant", "content": [{"type": "text", "text": "FINISH"}]}],
})
""",
    )

    result = RpcProcessSupervisor().run(
        RpcProcessSpec(
            command=[sys.executable, str(script)],
            log_dir=tmp_path / "logs",
            timeout_seconds=1,
            poll_interval_seconds=0.01,
            prompt="执行测试",
        )
    )

    assert result.returncode != 0
    assert result.last_assistant_text is None
    assert "before agent_settled" in result.stderr


def test_rpc_process_supervisor_rejects_eof_before_final_text_response(tmp_path) -> None:
    script = _rpc_script(
        tmp_path,
        """
prompt = read_command()
emit({"id": prompt["id"], "type": "response", "command": "prompt", "success": True})
emit({"type": "agent_settled"})
final_text = read_command()
record(final_text)
""",
    )

    result = RpcProcessSupervisor().run(
        RpcProcessSpec(
            command=[sys.executable, str(script)],
            log_dir=tmp_path / "logs",
            timeout_seconds=1,
            poll_interval_seconds=0.01,
            prompt="执行测试",
        )
    )

    assert result.returncode != 0
    assert "before final assistant text response" in result.stderr


def test_rpc_process_supervisor_rejects_failed_final_text_query(tmp_path) -> None:
    script = _rpc_script(
        tmp_path,
        """
prompt = read_command()
emit({"id": prompt["id"], "type": "response", "command": "prompt", "success": True})
emit({"type": "agent_settled"})
final_text = read_command()
emit({
    "id": final_text["id"],
    "type": "response",
    "command": "get_last_assistant_text",
    "success": False,
    "error": "state unavailable",
})
abort = read_command()
record(abort)
""",
    )

    result = RpcProcessSupervisor().run(
        RpcProcessSpec(
            command=[sys.executable, str(script)],
            log_dir=tmp_path / "logs",
            timeout_seconds=1,
            poll_interval_seconds=0.01,
            prompt="执行测试",
        )
    )

    assert result.returncode != 0
    assert "state unavailable" in result.stderr


def test_rpc_process_supervisor_preserves_nonzero_exit_after_settlement(tmp_path) -> None:
    script = _rpc_script(
        tmp_path,
        """
prompt = read_command()
emit({"id": prompt["id"], "type": "response", "command": "prompt", "success": True})
settle("FINISH")
raise SystemExit(7)
""",
    )

    result = RpcProcessSupervisor().run(
        RpcProcessSpec(
            command=[sys.executable, str(script)],
            log_dir=tmp_path / "logs",
            timeout_seconds=1,
            poll_interval_seconds=0.01,
            prompt="执行测试",
        )
    )

    assert result.returncode == 7
    assert result.last_assistant_text == "FINISH"


def test_rpc_process_supervisor_aborts_on_hard_timeout(tmp_path) -> None:
    command_log = tmp_path / "commands.jsonl"
    script = _rpc_script(
        tmp_path,
        """
prompt = read_command()
record(prompt)
emit({"id": prompt["id"], "type": "response", "command": "prompt", "success": True})
abort = read_command()
record(abort)
emit({"id": abort.get("id"), "type": "response", "command": "abort", "success": True})
""",
    )

    result = RpcProcessSupervisor().run(
        RpcProcessSpec(
            command=[sys.executable, str(script)],
            log_dir=tmp_path / "logs",
            timeout_seconds=0.1,
            terminate_grace_seconds=0.5,
            poll_interval_seconds=0.01,
            environment={"COMMAND_LOG": str(command_log)},
            prompt="执行测试",
        )
    )

    commands = [json.loads(line) for line in command_log.read_text().splitlines()]
    assert [command["type"] for command in commands] == ["prompt", "abort"]
    assert result.timed_out is True
    assert result.returncode != 0


def test_rpc_process_supervisor_aborts_when_lease_is_lost(tmp_path) -> None:
    command_log = tmp_path / "commands.jsonl"
    script = _rpc_script(
        tmp_path,
        """
prompt = read_command()
record(prompt)
emit({"id": prompt["id"], "type": "response", "command": "prompt", "success": True})
abort = read_command()
record(abort)
emit({"id": abort.get("id"), "type": "response", "command": "abort", "success": True})
""",
    )
    started = time.monotonic()

    result = RpcProcessSupervisor().run(
        RpcProcessSpec(
            command=[sys.executable, str(script)],
            log_dir=tmp_path / "logs",
            timeout_seconds=2,
            terminate_grace_seconds=0.5,
            poll_interval_seconds=0.01,
            environment={"COMMAND_LOG": str(command_log)},
            prompt="执行测试",
        ),
        should_cancel=lambda: "lease_lost" if time.monotonic() - started >= 0.05 else None,
    )

    commands = [json.loads(line) for line in command_log.read_text().splitlines()]
    assert [command["type"] for command in commands] == ["prompt", "abort"]
    assert result.cancelled is True
    assert result.cancel_reason == "lease_lost"
    assert result.timed_out is False


def test_rpc_process_supervisor_tolerates_a_rejected_soft_reminder(tmp_path) -> None:
    script = _rpc_script(
        tmp_path,
        """
prompt = read_command()
emit({"id": prompt["id"], "type": "response", "command": "prompt", "success": True})
steer = read_command()
emit({
    "id": steer["id"],
    "type": "response",
    "command": "steer",
    "success": False,
    "error": "busy",
})
emit({
    "type": "agent_end",
    "messages": [{"role": "assistant", "content": [{"type": "text", "text": "FINISH"}]}],
})
settle("FINISH")
""",
    )
    reminders: list[str] = []

    result = RpcProcessSupervisor().run(
        RpcProcessSpec(
            command=[sys.executable, str(script)],
            log_dir=tmp_path / "logs",
            timeout_seconds=1,
            poll_interval_seconds=0.01,
            prompt="执行测试",
            steer_after_seconds=0.05,
            steer_message="立即收口",
        ),
        on_steer=lambda: reminders.append("sent"),
    )

    assert result.returncode == 0
    assert result.last_assistant_text == "FINISH"
    assert reminders == []


def _rpc_script(tmp_path, body: str):
    path = tmp_path / f"rpc-{time.monotonic_ns()}.py"
    path.write_text(
        """\
import json
import os
import sys
import time

def read_command():
    line = sys.stdin.readline()
    if not line:
        raise SystemExit(0)
    return json.loads(line)

def emit(payload):
    print(json.dumps(payload, ensure_ascii=False), flush=True)

def record(payload):
    path = os.environ.get("COMMAND_LOG")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\\n")

def settle(text):
    emit({"type": "agent_settled"})
    command = read_command()
    record(command)
    if command.get("type") != "get_last_assistant_text":
        raise SystemExit(9)
    emit({
        "id": command.get("id"),
        "type": "response",
        "command": "get_last_assistant_text",
        "success": True,
        "data": {"text": text},
    })

"""
        + body,
        encoding="utf-8",
    )
    return path
