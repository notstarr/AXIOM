from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Protocol

from axiom.dispatcher.bootstrap import AxiomToolLoadError, TransientWorkerError
from axiom.dispatcher.closeout import bounded_closeout, make_closeout_progress
from axiom.dispatcher.heartbeat import LeaseHeartbeat, LeaseLostError
from axiom.dispatcher.http import (
    ControlPlaneError,
    input_with_lock_backoff,
    is_transient_control_plane_error,
)
from axiom.modules.execution.schemas import ActivityCommand, PlanExecutionInput, ReleaseIntentCommand
from axiom.modules.execution.stage_outputs import StageOutputError, require_finish
from axiom.modules.plan.schemas import (
    ClaimedPlanRun,
    PlanCommitResult,
    PlanWorkspace,
    PlanWorkspaceStatus,
)


logger = logging.getLogger(__name__)


class PlanExecutionTimeout(TimeoutError):
    """Plan did not finish inside its execution budget."""


class PlanDecisionError(ValueError):
    """Plan finished without a contract-compliant decision/workspace."""


class PlanExecutor(Protocol):
    def execute(
        self,
        execution_input: PlanExecutionInput,
        *,
        workspace: PlanWorkspace,
        lease_active: Callable[[], bool],
    ) -> object:
        ...

    def conclude(
        self,
        execution_input: PlanExecutionInput,
        *,
        workspace: PlanWorkspace,
        reason: str,
        lease_active: Callable[[], bool],
    ) -> object:
        ...


class PlanControlPlane(Protocol):
    def start(self, work: ClaimedPlanRun) -> None:
        ...

    def heartbeat(self, work: ClaimedPlanRun) -> None:
        ...

    def input(self, work: ClaimedPlanRun) -> PlanExecutionInput:
        ...

    def open_plan_workspace(
        self,
        work: ClaimedPlanRun,
        *,
        context_revision: int,
    ) -> PlanWorkspace:
        ...

    def activity(self, work: ClaimedPlanRun, command: ActivityCommand) -> None:
        ...

    def plan_workspace_status(
        self,
        work: ClaimedPlanRun,
        *,
        workspace_id: str,
    ) -> PlanWorkspaceStatus:
        ...

    def commit_plan_workspace(
        self,
        work: ClaimedPlanRun,
        *,
        workspace_id: str,
    ) -> PlanCommitResult:
        ...

    def begin_conclude(self, work: ClaimedPlanRun) -> None:
        ...

    def abandon_plan_workspace(
        self,
        work: ClaimedPlanRun,
        *,
        workspace_id: str,
        failure_reason: str,
    ) -> None:
        ...

    def release(self, work: ClaimedPlanRun, command: ReleaseIntentCommand) -> None:
        ...


class PlanRunner:
    """Run one independent Plan lease and commit its planning workspace.

    This intentionally mirrors the lease/closeout mechanics used by other
    stages while keeping Plan's domain protocol, workspace and failure labels
    separate from Reason.
    """

    def __init__(
        self,
        control_plane: PlanControlPlane,
        *,
        heartbeat_interval_seconds: float = 10.0,
        cancellation: threading.Event | None = None,
    ):
        self.control_plane = control_plane
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.cancellation = cancellation

    def run(self, work: ClaimedPlanRun, executor: PlanExecutor) -> PlanCommitResult:
        heartbeat = LeaseHeartbeat(
            lambda: self.control_plane.heartbeat(work),
            interval_seconds=self.heartbeat_interval_seconds,
            cancellation=self.cancellation,
        )
        workspace: PlanWorkspace | None = None
        try:
            self.control_plane.start(work)
            heartbeat.start()
            execution_input = input_with_lock_backoff(
                lambda: self.control_plane.input(work),
                ensure_active=heartbeat.ensure_active,
                describe=f"{work.task_id}/{work.plan_run_id}",
            )
            if execution_input.context_revision is None:
                raise RuntimeError("Plan execution requires a Context revision")
            workspace = self.control_plane.open_plan_workspace(
                work,
                context_revision=execution_input.context_revision,
            )
            self.control_plane.activity(
                work,
                ActivityCommand(
                    lease_id=work.lease_id,
                    activity_kind="analyzing",
                    activity_summary="正在进行 Plan 全景复盘并规划下一轮工作",
                    activity_tool="",
                ),
            )
            try:
                raw_output = executor.execute(
                    execution_input,
                    workspace=workspace,
                    lease_active=heartbeat.is_active,
                )
                self._validate_finish_and_workspace(work, workspace, raw_output)
            except (PlanExecutionTimeout, PlanDecisionError, StageOutputError) as exc:
                self._conclude(
                    work,
                    executor,
                    execution_input,
                    workspace,
                    heartbeat,
                    reason=str(exc),
                )
            heartbeat.ensure_active()
            return self.control_plane.commit_plan_workspace(
                work,
                workspace_id=workspace.id,
            )
        except LeaseLostError:
            self._close_failed_attempt(
                work,
                workspace,
                failure_kind="lease_heartbeat_lost",
                failure_class="transient",
            )
            raise
        except AxiomToolLoadError:
            self._close_failed_attempt(
                work,
                workspace,
                failure_kind="axiom_tool_load_failed",
                failure_class="transient",
            )
            raise
        except TransientWorkerError:
            self._close_failed_attempt(
                work,
                workspace,
                failure_kind="worker_infrastructure",
                failure_class="transient",
            )
            raise
        except (PlanExecutionTimeout, PlanDecisionError, StageOutputError):
            self._close_failed_attempt(
                work,
                workspace,
                failure_kind="plan_output_invalid",
                failure_class="execution",
            )
            raise
        except ControlPlaneError as exc:
            transient = is_transient_control_plane_error(exc)
            self._close_failed_attempt(
                work,
                workspace,
                failure_kind="control_plane_unavailable" if transient else "plan_execution_failed",
                failure_class="transient" if transient else "execution",
            )
            raise
        except BaseException:
            self._close_failed_attempt(
                work,
                workspace,
                failure_kind="plan_execution_failed",
                failure_class="execution",
            )
            raise
        finally:
            heartbeat.stop()

    def _conclude(
        self,
        work: ClaimedPlanRun,
        executor: PlanExecutor,
        execution_input: PlanExecutionInput,
        workspace: PlanWorkspace,
        heartbeat: LeaseHeartbeat,
        *,
        reason: str,
    ) -> None:
        heartbeat.ensure_active()
        bounded_closeout(
            # ``bounded_closeout`` only relies on the common timeout and
            # model_copy contract; PlanExecutionInput remains a separate
            # public schema and never becomes a Reason input.
            execution_input=execution_input,  # type: ignore[arg-type]
            begin=lambda: self.control_plane.begin_conclude(work),
            conclude=lambda attempt_input, attempt_reason: executor.conclude(
                attempt_input,  # type: ignore[arg-type]
                workspace=workspace,
                reason=attempt_reason,
                lease_active=heartbeat.is_active,
            ),
            validate=lambda _attempt_input, raw_output: self._validate_finish_and_workspace(
                work,
                workspace,
                raw_output,
            ),
            reason=reason,
            retry_errors=(PlanDecisionError, StageOutputError),
            default_timeout_seconds=120,
            progress=make_closeout_progress(
                self.control_plane.activity,
                work,
                verb="Plan 收口整理中：补齐 Workspace 校验缺口",
            ),
        )

    def _validate_finish_and_workspace(
        self,
        work: ClaimedPlanRun,
        workspace: PlanWorkspace,
        raw_output: object,
    ) -> None:
        try:
            require_finish(str(raw_output))
        except StageOutputError as exc:
            raise PlanDecisionError(str(exc)) from exc
        status = self.control_plane.plan_workspace_status(
            work,
            workspace_id=workspace.id,
        )
        if not status.ready:
            raise PlanDecisionError("；".join(status.errors) or "Plan Workspace 未就绪")

    def _close_failed_attempt(
        self,
        work: ClaimedPlanRun,
        workspace: PlanWorkspace | None,
        *,
        failure_kind: str,
        failure_class: str,
    ) -> None:
        if workspace is not None:
            try:
                self.control_plane.abandon_plan_workspace(
                    work,
                    workspace_id=workspace.id,
                    failure_reason=failure_kind,
                )
            except LeaseLostError:
                pass
            except BaseException:
                logger.exception("failed to abandon Plan Workspace %s", workspace.id)
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
                "failed to release PlanRun %s/%s after %s",
                work.task_id,
                work.plan_run_id,
                failure_kind,
            )
