from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Protocol

from axiom.dispatcher.bootstrap import AxiomToolLoadError, TransientWorkerError
from axiom.dispatcher.closeout import bounded_closeout
from axiom.dispatcher.heartbeat import LeaseHeartbeat, LeaseLostError
from axiom.modules.execution.schemas import ExecutionInput, ReleaseIntentCommand
from axiom.modules.execution.stage_outputs import StageOutputError, require_finish, review_files
from axiom.modules.vulnerability_reviews.schemas import (
    ClaimedVulnerabilityReviewRun,
    CompleteReviewCommand,
    ReviewStatus,
    VulnerabilityReviewCompleted,
)


logger = logging.getLogger(__name__)


class ReviewExecutionTimeout(TimeoutError):
    """Reviewer did not finish inside its execution budget."""


class ReviewDecisionError(ValueError):
    """Reviewer finished without a contract-compliant structured decision."""


class ReviewExecutor(Protocol):
    def execute(
        self,
        execution_input: ExecutionInput,
        *,
        lease_active: Callable[[], bool],
    ) -> object:
        ...

    def conclude(
        self,
        execution_input: ExecutionInput,
        *,
        reason: str,
        lease_active: Callable[[], bool],
    ) -> object:
        ...


class ReviewControlPlane(Protocol):
    def start(self, work: ClaimedVulnerabilityReviewRun) -> None:
        ...

    def heartbeat(self, work: ClaimedVulnerabilityReviewRun) -> None:
        ...

    def input(self, work: ClaimedVulnerabilityReviewRun) -> ExecutionInput:
        ...

    def begin_conclude(self, work: ClaimedVulnerabilityReviewRun) -> None:
        ...

    def review_status(self, work: ClaimedVulnerabilityReviewRun) -> ReviewStatus:
        ...

    def complete_review(
        self,
        work: ClaimedVulnerabilityReviewRun,
        command: CompleteReviewCommand,
    ) -> VulnerabilityReviewCompleted:
        ...

    def release(
        self,
        work: ClaimedVulnerabilityReviewRun,
        command: ReleaseIntentCommand,
    ) -> None:
        ...


class ReviewRunner:
    """Review one vulnerability and atomically persist its decision and artifacts."""

    def __init__(
        self,
        control_plane: ReviewControlPlane,
        *,
        heartbeat_interval_seconds: float = 10.0,
        cancellation: threading.Event | None = None,
    ):
        self.control_plane = control_plane
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.cancellation = cancellation

    def run(
        self,
        work: ClaimedVulnerabilityReviewRun,
        executor: ReviewExecutor,
    ) -> VulnerabilityReviewCompleted:
        heartbeat = LeaseHeartbeat(
            lambda: self.control_plane.heartbeat(work),
            interval_seconds=self.heartbeat_interval_seconds,
            cancellation=self.cancellation,
        )
        try:
            self.control_plane.start(work)
            heartbeat.start()
            execution_input = self.control_plane.input(work)
            try:
                raw_output = executor.execute(
                    execution_input,
                    lease_active=heartbeat.is_active,
                )
                self._validate_output(work, execution_input, raw_output)
            except (ReviewExecutionTimeout, ReviewDecisionError, StageOutputError) as exc:
                self._conclude(
                    work,
                    executor,
                    execution_input,
                    heartbeat,
                    reason=str(exc),
                )
            heartbeat.ensure_active()
            return self.control_plane.complete_review(
                work,
                CompleteReviewCommand(lease_id=work.lease_id),
            )
        except LeaseLostError:
            self._release_best_effort(
                work,
                failure_kind="lease_heartbeat_lost",
                failure_class="transient",
            )
            raise
        except AxiomToolLoadError:
            self._release_best_effort(
                work,
                failure_kind="axiom_tool_load_failed",
                failure_class="transient",
            )
            raise
        except TransientWorkerError:
            self._release_best_effort(
                work,
                failure_kind="worker_infrastructure",
                failure_class="transient",
            )
            raise
        except (ReviewExecutionTimeout, ReviewDecisionError, StageOutputError):
            self._release_best_effort(
                work,
                failure_kind="review_output_invalid",
                failure_class="execution",
            )
            raise
        except BaseException:
            self._release_best_effort(
                work,
                failure_kind="review_execution_failed",
                failure_class="execution",
            )
            raise
        finally:
            heartbeat.stop()

    def _conclude(
        self,
        work: ClaimedVulnerabilityReviewRun,
        executor: ReviewExecutor,
        execution_input: ExecutionInput,
        heartbeat: LeaseHeartbeat,
        *,
        reason: str,
    ) -> None:
        heartbeat.ensure_active()
        bounded_closeout(
            execution_input=execution_input,
            begin=lambda: self.control_plane.begin_conclude(work),
            conclude=lambda attempt_input, attempt_reason: executor.conclude(
                attempt_input,
                reason=attempt_reason,
                lease_active=heartbeat.is_active,
            ),
            validate=lambda attempt_input, raw_output: self._validate_output(
                work,
                attempt_input,
                raw_output,
            ),
            reason=reason,
            retry_errors=(ReviewDecisionError, StageOutputError),
            default_timeout_seconds=120,
        )

    def _validate_output(
        self,
        work: ClaimedVulnerabilityReviewRun,
        execution_input: ExecutionInput,
        raw_output: object,
    ) -> None:
        require_finish(str(raw_output))
        review_files(execution_input.run_dir, emit_warnings=False)
        status = self.control_plane.review_status(work)
        if not status.ready:
            raise ReviewDecisionError("；".join(status.errors) or "Reviewer 裁决未就绪")

    def _release_best_effort(
        self,
        work: ClaimedVulnerabilityReviewRun,
        *,
        failure_kind: str,
        failure_class: str,
    ) -> None:
        try:
            self.control_plane.release(
                work,
                ReleaseIntentCommand(
                    lease_id=work.lease_id,
                    failure_kind=failure_kind,
                    failure_class=failure_class,
                ),
            )
        except BaseException:
            logger.exception(
                "failed to release Vulnerability ReviewRun %s/%s after %s",
                work.task_id,
                work.review_run_id,
                failure_kind,
            )
