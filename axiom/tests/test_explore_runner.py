from __future__ import annotations

import logging
from pathlib import Path

import pytest

from axiom.dispatcher.explore import ExploreRunner
from axiom.modules.execution.schemas import ExecutionInput
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
