from __future__ import annotations

from pathlib import Path

import pytest

from axiom.dispatcher.review import (
    ReviewDecisionError,
    ReviewExecutionTimeout,
    ReviewRunner,
)
from axiom.modules.execution.schemas import ExecutionInput, ReleaseIntentCommand
from axiom.modules.execution.stage_outputs import StageOutputError
from axiom.modules.vulnerability_reviews.schemas import (
    ClaimedVulnerabilityReviewRun,
    ReviewStatus,
    VulnerabilityReviewCompleted,
)


def _execution_input(tmp_path: Path) -> ExecutionInput:
    return ExecutionInput(
        task_id="t_001",
        project_id="p_001",
        review_run_id="vr000001",
        vulnerability_id="v001",
        lease_id="lease-review",
        worker_name="reviewer-a",
        intent_kind="review",
        intent_description="审核待验证漏洞",
        origin="review.example.com",
        goal="验证漏洞",
        scope={"enabled": True, "targets": ["review.example.com"]},
        run_dir=str(tmp_path),
        execute_timeout_seconds=300,
        conclude_timeout_seconds=30,
    )


def _work() -> ClaimedVulnerabilityReviewRun:
    return ClaimedVulnerabilityReviewRun(
        task_id="t_001",
        review_run_id="vr000001",
        vulnerability_id="v001",
        worker_name="reviewer-a",
        claimed_at="2026-08-04T00:00:00Z",
        lease_id="lease-review",
    )


def _write_review(run_dir: str) -> None:
    root = Path(run_dir) / "artifacts"
    root.mkdir(parents=True, exist_ok=True)
    (root / "review.md").write_text("# 审核\n\n证据充分。\n", encoding="utf-8")


class _ControlPlane:
    def __init__(self, execution_input: ExecutionInput, *, ready: bool = True) -> None:
        self.execution_input = execution_input
        self.ready = ready
        self.started = 0
        self.began_conclude = 0
        self.completed = 0
        self.released: list[ReleaseIntentCommand] = []

    def start(self, _work: ClaimedVulnerabilityReviewRun) -> None:
        self.started += 1

    def heartbeat(self, _work: ClaimedVulnerabilityReviewRun) -> None:
        return None

    def input(self, _work: ClaimedVulnerabilityReviewRun) -> ExecutionInput:
        return self.execution_input

    def begin_conclude(self, _work: ClaimedVulnerabilityReviewRun) -> None:
        self.began_conclude += 1

    def review_status(self, _work: ClaimedVulnerabilityReviewRun) -> ReviewStatus:
        return ReviewStatus(
            review_run_id="vr000001",
            task_id="t_001",
            vulnerability_id="v001",
            status="running",
            decision="verified" if self.ready else None,
            decision_reason="证据充分" if self.ready else "",
            ready=self.ready,
            errors=[] if self.ready else ["尚未提交结构化审核裁决"],
        )

    def complete_review(
        self,
        _work: ClaimedVulnerabilityReviewRun,
        _command,
    ) -> VulnerabilityReviewCompleted:
        self.completed += 1
        return VulnerabilityReviewCompleted(
            task_id="t_001",
            review_run_id="vr000001",
            vulnerability_id="v001",
            decision="verified",
            vulnerability_status="已验证",
            artifacts=[],
        )

    def release(
        self,
        _work: ClaimedVulnerabilityReviewRun,
        command: ReleaseIntentCommand,
    ) -> None:
        self.released.append(command)


class _Executor:
    def __init__(
        self,
        control: _ControlPlane,
        *,
        execute_result: object,
        conclude_result: object = "FINISH",
        write_on_execute: bool = True,
        repair_on_conclude: bool = False,
    ) -> None:
        self.control = control
        self.execute_result = execute_result
        self.conclude_result = conclude_result
        self.write_on_execute = write_on_execute
        self.repair_on_conclude = repair_on_conclude
        self.execute_calls = 0
        self.conclude_calls = 0

    def execute(self, execution_input: ExecutionInput, *, lease_active) -> object:
        assert lease_active()
        self.execute_calls += 1
        if self.write_on_execute:
            _write_review(execution_input.run_dir)
        if isinstance(self.execute_result, BaseException):
            raise self.execute_result
        return self.execute_result

    def conclude(
        self,
        execution_input: ExecutionInput,
        *,
        reason: str,
        lease_active,
    ) -> object:
        assert lease_active()
        assert reason
        self.conclude_calls += 1
        if self.repair_on_conclude:
            _write_review(execution_input.run_dir)
            self.control.ready = True
        if isinstance(self.conclude_result, BaseException):
            raise self.conclude_result
        return self.conclude_result


def test_review_runner_completes_only_after_strict_finish_artifact_and_decision(
    tmp_path: Path,
) -> None:
    execution_input = _execution_input(tmp_path)
    control = _ControlPlane(execution_input)
    executor = _Executor(control, execute_result="FINISH")

    completed = ReviewRunner(control, heartbeat_interval_seconds=60).run(
        _work(),
        executor,
    )

    assert completed.status == "completed"
    assert completed.decision == "verified"
    assert control.started == 1
    assert control.completed == 1
    assert control.began_conclude == 0
    assert control.released == []


def test_review_runner_uses_bounded_closeout_to_repair_timeout_outputs_and_decision(
    tmp_path: Path,
) -> None:
    execution_input = _execution_input(tmp_path)
    control = _ControlPlane(execution_input, ready=False)
    executor = _Executor(
        control,
        execute_result=ReviewExecutionTimeout("execute budget exhausted"),
        write_on_execute=False,
        repair_on_conclude=True,
    )

    completed = ReviewRunner(control, heartbeat_interval_seconds=60).run(
        _work(),
        executor,
    )

    assert completed.status == "completed"
    assert executor.conclude_calls == 1
    assert control.began_conclude == 1
    assert control.released == []


def test_review_runner_rejects_non_exact_finish_and_releases_after_closeout_retries(
    tmp_path: Path,
) -> None:
    execution_input = _execution_input(tmp_path)
    control = _ControlPlane(execution_input)
    executor = _Executor(
        control,
        execute_result="FINISH\n审核完成",
        conclude_result="`FINISH`",
    )

    with pytest.raises(StageOutputError, match="仅包含 FINISH"):
        ReviewRunner(control, heartbeat_interval_seconds=60).run(_work(), executor)

    assert executor.conclude_calls == 3
    assert control.completed == 0
    assert len(control.released) == 1
    assert control.released[0].failure_kind == "review_output_invalid"
    assert control.released[0].failure_class == "execution"


def test_review_runner_does_not_allow_finish_to_bypass_missing_decision(
    tmp_path: Path,
) -> None:
    execution_input = _execution_input(tmp_path)
    control = _ControlPlane(execution_input, ready=False)
    executor = _Executor(
        control,
        execute_result="FINISH",
        conclude_result="FINISH",
    )

    with pytest.raises(ReviewDecisionError, match="尚未提交"):
        ReviewRunner(control, heartbeat_interval_seconds=60).run(_work(), executor)

    assert executor.conclude_calls == 3
    assert control.completed == 0
    assert control.released[0].failure_kind == "review_output_invalid"
