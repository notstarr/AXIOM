from __future__ import annotations

import json
import sys
import textwrap
import time

from axiom.dispatcher.runtime.rpc import ManagedRpcProcess


def test_rpc_process_steers_before_hard_deadline(tmp_path) -> None:
    script = tmp_path / "rpc_agent.py"
    script.write_text(
        textwrap.dedent(
            """
            import json, sys, threading, time

            def read_commands():
                for line in sys.stdin:
                    command = json.loads(line)
                    if command["type"] == "prompt":
                        print(json.dumps({"type": "agent_start"}), flush=True)
                    elif command["type"] == "steer":
                        print(json.dumps({"type": "agent_end", "messages": []}), flush=True)
                        return
                    elif command["type"] == "abort":
                        return

            threading.Thread(target=read_commands, daemon=True).start()
            while True:
                time.sleep(0.1)
            """
        ),
        encoding="utf-8",
    )
    process = ManagedRpcProcess([sys.executable, str(script)], {})
    deadlines = []
    process.set_deadline_callback(lambda event, payload: deadlines.append((event, payload)))
    process.start()
    try:
        result = process.prompt(
            "explore",
            soft_timeout=0.1,
            hard_timeout=2,
            steering_message="conclude now",
        )
    finally:
        process.stop()

    assert result.returncode == 0
    assert result.soft_steered is True
    assert result.timed_out is False
    assert json.loads(result.stdout.splitlines()[-1])["type"] == "agent_end"
    assert deadlines and deadlines[0][0] == "soft_deadline"


def test_rpc_process_uses_hard_deadline_when_agent_ignores_abort(monkeypatch, tmp_path) -> None:
    import axiom.dispatcher.runtime.rpc as rpc_module

    monkeypatch.setattr(rpc_module, "RPC_ABORT_GRACE_SECONDS", 0.1)
    script = tmp_path / "rpc_stuck_agent.py"
    script.write_text(
        textwrap.dedent(
            """
            import time
            while True:
                time.sleep(0.1)
            """
        ),
        encoding="utf-8",
    )
    process = ManagedRpcProcess([sys.executable, str(script)], {})
    deadlines = []
    process.set_deadline_callback(lambda event, payload: deadlines.append((event, payload)))
    process.start()
    try:
        result = process.prompt(
            "explore",
            soft_timeout=None,
            hard_timeout=0.2,
        )
    finally:
        process.stop()

    assert result.timed_out is True
    assert result.soft_steered is False
    assert deadlines and deadlines[-1][0] == "hard_deadline"


def test_rpc_process_aborts_stuck_turn_after_steer_grace(tmp_path) -> None:
    script = tmp_path / "rpc_abortable_agent.py"
    script.write_text(
        textwrap.dedent(
            """
            import json, sys, threading, time

            def read_commands():
                for line in sys.stdin:
                    command = json.loads(line)
                    if command["type"] == "prompt":
                        print(json.dumps({"type": "agent_start"}), flush=True)
                    elif command["type"] == "abort":
                        print(json.dumps({"type": "agent_end", "messages": []}), flush=True)
                        return

            threading.Thread(target=read_commands, daemon=True).start()
            while True:
                time.sleep(0.1)
            """
        ),
        encoding="utf-8",
    )
    process = ManagedRpcProcess([sys.executable, str(script)], {})
    deadlines = []
    process.set_deadline_callback(lambda event, payload: deadlines.append((event, payload)))
    process.start()
    started = time.monotonic()
    try:
        result = process.prompt(
            "explore",
            soft_timeout=0.05,
            hard_timeout=2,
            steering_message="conclude",
            abort_after_steer=0.05,
        )
    finally:
        process.stop()

    elapsed = time.monotonic() - started
    assert result.soft_steered is True
    assert result.soft_aborted is True
    assert result.timed_out is False
    assert elapsed >= 0.09
    assert [event for event, _payload in deadlines[:2]] == ["soft_deadline", "soft_abort"]


def test_rpc_process_does_not_abort_while_events_show_progress(tmp_path) -> None:
    script = tmp_path / "rpc_active_agent.py"
    script.write_text(
        textwrap.dedent(
            """
            import json, sys, threading, time

            commands = []

            def read_commands():
                for line in sys.stdin:
                    commands.append(json.loads(line)["type"])

            threading.Thread(target=read_commands, daemon=True).start()
            while "prompt" not in commands:
                time.sleep(0.005)
            print(json.dumps({"type": "agent_start"}), flush=True)
            for index in range(8):
                time.sleep(0.03)
                print(json.dumps({"type": "tool_result", "index": index}), flush=True)
            print(json.dumps({"type": "agent_end", "messages": []}), flush=True)
            time.sleep(0.2)
            """
        ),
        encoding="utf-8",
    )
    process = ManagedRpcProcess([sys.executable, str(script)], {})
    process.start()
    try:
        result = process.prompt(
            "explore",
            soft_timeout=0.02,
            hard_timeout=1,
            steering_message="conclude",
            abort_after_steer=0.05,
        )
    finally:
        process.stop()

    assert result.soft_steered is True
    assert result.soft_aborted is False
    assert result.timed_out is False


def test_rpc_process_aborts_only_after_events_become_idle(tmp_path) -> None:
    script = tmp_path / "rpc_idle_after_progress_agent.py"
    script.write_text(
        textwrap.dedent(
            """
            import json, sys, threading, time

            def read_commands():
                for line in sys.stdin:
                    command = json.loads(line)
                    if command["type"] == "prompt":
                        print(json.dumps({"type": "agent_start"}), flush=True)
                        for index in range(3):
                            time.sleep(0.03)
                            print(json.dumps({"type": "tool_result", "index": index}), flush=True)
                    elif command["type"] == "abort":
                        print(json.dumps({"type": "agent_end", "messages": []}), flush=True)
                        return

            threading.Thread(target=read_commands, daemon=True).start()
            while True:
                time.sleep(0.1)
            """
        ),
        encoding="utf-8",
    )
    process = ManagedRpcProcess([sys.executable, str(script)], {})
    process.start()
    started = time.monotonic()
    try:
        result = process.prompt(
            "explore",
            soft_timeout=0.01,
            hard_timeout=1,
            steering_message="conclude",
            abort_after_steer=0.06,
        )
    finally:
        process.stop()

    elapsed = time.monotonic() - started
    assert result.soft_steered is True
    assert result.soft_aborted is True
    assert result.timed_out is False
    # Three progress events take about 90 ms. The idle grace starts after the
    # final event, so a fixed delay from steer would finish much earlier.
    assert elapsed >= 0.13


def test_rpc_follow_up_starts_a_new_turn_after_agent_is_idle(tmp_path) -> None:
    script = tmp_path / "rpc_two_turn_agent.py"
    script.write_text(
        textwrap.dedent(
            """
            import json, sys, threading, time

            def read_commands():
                turn = 0
                for line in sys.stdin:
                    command = json.loads(line)
                    if command["type"] != "prompt":
                        continue
                    turn += 1
                    print(json.dumps({"type": "agent_start", "turn": turn}), flush=True)
                    print(json.dumps({"type": "agent_end", "turn": turn, "messages": []}), flush=True)

            threading.Thread(target=read_commands, daemon=True).start()
            while True:
                time.sleep(0.1)
            """
        ),
        encoding="utf-8",
    )
    process = ManagedRpcProcess([sys.executable, str(script)], {})
    process.start()
    try:
        first = process.prompt("explore", soft_timeout=None, hard_timeout=1)
        second = process.follow_up("conclude", hard_timeout=1)
    finally:
        process.stop()

    assert first.timed_out is False
    assert second.timed_out is False
    events = [json.loads(line) for line in second.stdout.splitlines()]
    assert any(event.get("type") == "agent_end" and event.get("turn") == 2 for event in events)


def test_rpc_process_forwards_structured_events_to_callback(tmp_path) -> None:
    script = tmp_path / "rpc_tool_agent.py"
    script.write_text(
        textwrap.dedent(
            """
            import json, sys, threading, time

            def read_commands():
                for line in sys.stdin:
                    command = json.loads(line)
                    if command["type"] != "prompt":
                        continue
                    print(json.dumps({"type": "agent_start"}), flush=True)
                    print(json.dumps({
                        "type": "tool_execution_start",
                        "toolCallId": "call-1",
                        "toolName": "bash",
                        "args": {"command": "nmap example.com"}
                    }), flush=True)
                    print(json.dumps({"type": "tool_execution_end", "toolCallId": "call-1"}), flush=True)
                    print(json.dumps({"type": "agent_end", "messages": []}), flush=True)

            threading.Thread(target=read_commands, daemon=True).start()
            while True:
                time.sleep(0.1)
            """
        ),
        encoding="utf-8",
    )
    events = []
    process = ManagedRpcProcess([sys.executable, str(script)], {})
    process.set_event_callback(events.append)
    process.start()
    try:
        result = process.prompt("explore", soft_timeout=None, hard_timeout=1)
    finally:
        process.stop()

    assert result.timed_out is False
    assert [event["type"] for event in events] == [
        "agent_start",
        "tool_execution_start",
        "tool_execution_end",
        "agent_end",
    ]
