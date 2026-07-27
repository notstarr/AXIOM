from __future__ import annotations

import pytest

from axiom.dispatcher.closeout import bounded_closeout
from axiom.modules.execution.schemas import ExecutionInput
from axiom.modules.execution.stage_outputs import StageOutputError, require_finish


def _input(tmp_path) -> ExecutionInput:
    return ExecutionInput(
        task_id="t_001",
        intent_id="i001",
        lease_id="lease_test",
        worker_name="worker-a",
        origin="https://a.example.com",
        goal="完成测试",
        scope={"enabled": True, "targets": ["a.example.com"], "exclude": []},
        run_dir=str(tmp_path),
        conclude_timeout_seconds=10,
    )


def test_bounded_closeout_retries_contract_only_in_the_same_session(tmp_path) -> None:
    events: list[object] = []
    outputs = ["说明\nFINISH", "FINISH"]

    bounded_closeout(
        execution_input=_input(tmp_path),
        begin=lambda: events.append("begin"),
        conclude=lambda value, reason: events.append(
            (value.conclude_timeout_seconds, reason)
        )
        or outputs.pop(0),
        validate=lambda _value, output: require_finish(output),
        reason="最终响应必须仅包含 FINISH",
        retry_errors=(StageOutputError,),
        default_timeout_seconds=120,
    )

    attempts = [item for item in events if isinstance(item, tuple)]
    assert events[0] == "begin"
    assert len(attempts) == 2
    assert attempts[0][0] <= 10
    assert attempts[1][0] <= attempts[0][0]
    assert "第 2/3 次收口" in attempts[1][1]


def test_bounded_closeout_stops_after_three_invalid_contract_attempts(tmp_path) -> None:
    calls = 0

    def conclude(_value, _reason):
        nonlocal calls
        calls += 1
        return "仍然附带说明\nFINISH"

    with pytest.raises(StageOutputError, match="FINISH"):
        bounded_closeout(
            execution_input=_input(tmp_path),
            begin=lambda: None,
            conclude=conclude,
            validate=lambda _value, output: require_finish(output),
            reason="最终响应必须仅包含 FINISH",
            retry_errors=(StageOutputError,),
            default_timeout_seconds=120,
        )

    assert calls == 3
