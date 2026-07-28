from __future__ import annotations

import logging
from pathlib import Path

import pytest

from axiom.dispatcher.explore import ExploreExecutionTimeout, ExploreRunner
from axiom.dispatcher.schemas import ClaimedIntent
from axiom.modules.execution.schemas import (
    CompleteExploreCommand,
    ExecutionInput,
    ExploreCompleted,
    ReleaseIntentCommand,
)
from axiom.modules.execution.stage_outputs import StageOutputError


def _execution_input(tmp_path: Path) -> ExecutionInput:
    return ExecutionInput(
        task_id="t_001",
        project_id="p_001",
        intent_id="i002",
        lease_id="lease_test",
        worker_name="worker-a",
        intent_kind="explore",
        origin="a.example.com",
        goal="验证目标",
        scope={},
        run_dir=str(tmp_path),
    )


def _write_outputs(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "findings.md").write_text("已确认事实", encoding="utf-8")
    (artifacts / "leads.md").write_text("本轮无遗留待验证线索", encoding="utf-8")


def test_explore_normalized_finish_logs_execution_context(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    execution_input = _execution_input(tmp_path)
    _write_outputs(tmp_path)

    with caplog.at_level(logging.WARNING, logger="axiom.dispatcher.explore"):
        ExploreRunner._validate_output(execution_input, "检查完成。\nFINISH")

    assert "explore_finish_normalized" in caplog.text
    assert "task_id=t_001" in caplog.text
    assert "intent_id=i002" in caplog.text
    assert "lease_id=lease_test" in caplog.text
    assert "normalization=trailing_line" in caplog.text


def test_explore_exact_finish_does_not_log_normalization_warning(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    execution_input = _execution_input(tmp_path)
    _write_outputs(tmp_path)

    with caplog.at_level(logging.WARNING, logger="axiom.dispatcher.explore"):
        ExploreRunner._validate_output(execution_input, "FINISH")

    assert "explore_finish_normalized" not in caplog.text


def test_explore_finish_normalization_does_not_bypass_artifact_validation(
    tmp_path: Path,
) -> None:
    execution_input = _execution_input(tmp_path)

    with pytest.raises(StageOutputError, match="artifacts"):
        ExploreRunner._validate_output(execution_input, "`FINISH`")


@pytest.mark.parametrize(
    "historical_output",
    [
        "`FINISH`",
        "两个核心文件均存在且非空。\n\nFINISH",
        "证据文件均已就绪，无需补报漏洞。\n\nFINISH",
        "本轮无新增确认漏洞。\n\nFINISH",
    ],
)
def test_historical_explore_finish_failures_are_normalized(
    tmp_path: Path,
    historical_output: str,
) -> None:
    execution_input = _execution_input(tmp_path)
    _write_outputs(tmp_path)

    ExploreRunner._validate_output(execution_input, historical_output)


class _ControlPlane:
    def __init__(self, execution_input: ExecutionInput) -> None:
        self.execution_input = execution_input
        self.began_conclude = 0
        self.completed = 0
        self.released: list[ReleaseIntentCommand] = []

    def start(self, _work: ClaimedIntent) -> None:
        return None

    def heartbeat(self, _work: ClaimedIntent) -> None:
        return None

    def input(self, _work: ClaimedIntent) -> ExecutionInput:
        return self.execution_input

    def begin_conclude(self, _work: ClaimedIntent) -> None:
        self.began_conclude += 1

    def complete_explore(
        self,
        work: ClaimedIntent,
        _command: CompleteExploreCommand,
    ) -> ExploreCompleted:
        self.completed += 1
        return ExploreCompleted(
            task_id=work.task_id,
            intent_id=work.intent_id,
            fact_id="f001",
            artifacts=[],
        )

    def release(self, _work: ClaimedIntent, command: ReleaseIntentCommand) -> None:
        self.released.append(command)


class _CloseoutExecutor:
    def __init__(self, run_dir: Path, *, write_outputs: bool, conclude_result: object) -> None:
        self.run_dir = run_dir
        self.write_outputs = write_outputs
        self.conclude_result = conclude_result
        self.conclude_calls = 0

    def execute(self, _execution_input: ExecutionInput, *, lease_active) -> object:
        assert lease_active()
        if self.write_outputs:
            _write_outputs(self.run_dir)
        raise ExploreExecutionTimeout("execute budget exhausted")

    def conclude(
        self,
        _execution_input: ExecutionInput,
        *,
        reason: str,
        lease_active,
    ) -> object:
        assert reason
        assert lease_active()
        self.conclude_calls += 1
        if isinstance(self.conclude_result, BaseException):
            raise self.conclude_result
        return self.conclude_result


def _claimed_intent() -> ClaimedIntent:
    return ClaimedIntent(
        task_id="t_001",
        intent_id="i002",
        worker_name="worker-a",
        kind="explore",
        claimed_at="2026-07-28T00:00:00Z",
        lease_id="lease_test",
    )


@pytest.mark.parametrize(
    "conclude_result",
    [
        ExploreExecutionTimeout("conclude budget exhausted"),
        "仍在整理，没有完成标记",
    ],
)
def test_explore_closeout_completes_from_valid_core_outputs_without_finish(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    conclude_result: object,
) -> None:
    execution_input = _execution_input(tmp_path)
    control = _ControlPlane(execution_input)
    executor = _CloseoutExecutor(
        tmp_path,
        write_outputs=True,
        conclude_result=conclude_result,
    )

    with caplog.at_level(logging.WARNING, logger="axiom.dispatcher.explore"):
        completed = ExploreRunner(control, heartbeat_interval_seconds=60).run(
            _claimed_intent(),
            executor,
        )

    assert completed.status == "completed"
    assert control.completed == 1
    assert control.released == []
    assert control.began_conclude == 1
    assert "explore_closeout_core_fallback" in caplog.text
    assert "finish_confirmed=false" in caplog.text


def test_explore_closeout_accepts_explicitly_incomplete_core_summary(
    tmp_path: Path,
) -> None:
    execution_input = _execution_input(tmp_path)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "findings.md").write_text(
        "本轮测试未完成；已确认登录接口可访问。",
        encoding="utf-8",
    )
    (artifacts / "leads.md").write_text(
        "JWT 刷新与越权分支尚未验证，后续从现有登录态继续。",
        encoding="utf-8",
    )
    control = _ControlPlane(execution_input)
    executor = _CloseoutExecutor(
        tmp_path,
        write_outputs=False,
        conclude_result="收口进程没有返回完成标记",
    )

    completed = ExploreRunner(control, heartbeat_interval_seconds=60).run(
        _claimed_intent(),
        executor,
    )

    assert completed.status == "completed"
    assert control.completed == 1
    assert control.released == []


def test_explore_closeout_still_releases_when_core_outputs_are_missing(
    tmp_path: Path,
) -> None:
    execution_input = _execution_input(tmp_path)
    control = _ControlPlane(execution_input)
    executor = _CloseoutExecutor(
        tmp_path,
        write_outputs=False,
        conclude_result=ExploreExecutionTimeout("conclude budget exhausted"),
    )

    with pytest.raises(StageOutputError, match="artifacts|核心产物"):
        ExploreRunner(control, heartbeat_interval_seconds=60).run(
            _claimed_intent(),
            executor,
        )

    assert control.completed == 0
    assert len(control.released) == 1
    assert control.released[0].failure_kind == "explore_execution_failed"
