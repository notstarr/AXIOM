from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Protocol

from axiom.dispatcher.bootstrap import AxiomToolLoadError, TransientWorkerError
from axiom.dispatcher.closeout import bounded_closeout, make_closeout_progress
from axiom.dispatcher.heartbeat import LeaseHeartbeat, LeaseLostError
from axiom.dispatcher.http import ControlPlaneError, is_transient_control_plane_error
from axiom.modules.execution.schemas import (
    ActivityCommand,
    ExecutionInput,
    ReleaseIntentCommand,
)
from axiom.modules.execution.stage_outputs import StageOutputError, require_finish
from axiom.modules.reason.schemas import (
    ClaimedReasonRun,
    CommitResult,
    ReasonWorkspace,
    WorkspaceStatus,
)


logger = logging.getLogger(__name__)


class ReasonExecutionTimeout(TimeoutError):
    """Reason did not produce a valid plan inside its execution budget."""


class ReasonPlanError(ValueError):
    """Reason finished but did not persist a contract-compliant plan."""


class ReasonExecutor(Protocol):
    def execute(
        self,
        execution_input: ExecutionInput,
        *,
        workspace: ReasonWorkspace,
        lease_active: Callable[[], bool],
    ) -> object:
        ...

    def conclude(
        self,
        execution_input: ExecutionInput,
        *,
        workspace: ReasonWorkspace,
        reason: str,
        lease_active: Callable[[], bool],
    ) -> object:
        ...


class ReasonControlPlane(Protocol):
    def start(self, work: ClaimedReasonRun) -> None:
        ...

    def heartbeat(self, work: ClaimedReasonRun) -> None:
        ...

    def input(self, work: ClaimedReasonRun) -> ExecutionInput:
        ...

    def open_reason_workspace(
        self,
        work: ClaimedReasonRun,
        *,
        context_revision: int,
    ) -> ReasonWorkspace:
        ...

    def activity(self, work: ClaimedReasonRun, command: ActivityCommand) -> None:
        ...

    def reason_workspace_status(
        self,
        work: ClaimedReasonRun,
        *,
        workspace_id: str,
    ) -> WorkspaceStatus:
        ...

    def commit_reason_workspace(
        self,
        work: ClaimedReasonRun,
        *,
        workspace_id: str,
    ) -> CommitResult:
        ...

    def begin_conclude(self, work: ClaimedReasonRun) -> None:
        ...

    def abandon_reason_workspace(
        self,
        work: ClaimedReasonRun,
        *,
        workspace_id: str,
        failure_reason: str,
    ) -> None:
        ...

    def release(self, work: ClaimedReasonRun, command: ReleaseIntentCommand) -> None:
        ...


class ReasonRunner:
    """Plan once and atomically commit Todo/Intent mutations under one lease."""

    def __init__(
        self,
        control_plane: ReasonControlPlane,
        *,
        heartbeat_interval_seconds: float = 10.0,
        cancellation: threading.Event | None = None,
    ):
        self.control_plane = control_plane
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.cancellation = cancellation

    def run(self, work: ClaimedReasonRun, executor: ReasonExecutor) -> CommitResult:
        heartbeat = LeaseHeartbeat(
            lambda: self.control_plane.heartbeat(work),
            interval_seconds=self.heartbeat_interval_seconds,
            cancellation=self.cancellation,
        )
        workspace: ReasonWorkspace | None = None
        try:
            self.control_plane.start(work)
            heartbeat.start()
            execution_input = self.control_plane.input(work)
            if execution_input.context_revision is None:
                raise RuntimeError("Reason execution requires a Context revision")
            workspace = self.control_plane.open_reason_workspace(
                work,
                context_revision=execution_input.context_revision,
            )
            self.control_plane.activity(
                work,
                ActivityCommand(
                    lease_id=work.lease_id,
                    activity_kind="analyzing",
                    activity_summary="正在分析任务上下文并规划下一批 Intent",
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
            except (ReasonExecutionTimeout, ReasonPlanError, StageOutputError) as exc:
                self._conclude(
                    work,
                    executor,
                    execution_input,
                    workspace,
                    heartbeat,
                    reason=str(exc),
                )
            heartbeat.ensure_active()
            return self.control_plane.commit_reason_workspace(
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
        except (ReasonExecutionTimeout, ReasonPlanError, StageOutputError):
            # Workspace 延续：规划类失败是可重试的执行失败，draft 保留给
            # 下一次尝试续用（open 时按 run 认领孤儿工作区），不在这里弃置。
            self._close_failed_attempt(
                work,
                workspace,
                failure_kind="reason_plan_invalid",
                failure_class="execution",
                keep_workspace=True,
            )
            raise
        except ControlPlaneError as exc:
            transient = is_transient_control_plane_error(exc)
            self._close_failed_attempt(
                work,
                workspace,
                failure_kind=(
                    "control_plane_unavailable"
                    if transient
                    else "reason_execution_failed"
                ),
                failure_class="transient" if transient else "execution",
            )
            raise
        except BaseException:
            self._close_failed_attempt(
                work,
                workspace,
                failure_kind="reason_execution_failed",
                failure_class="execution",
            )
            raise
        finally:
            heartbeat.stop()

    def _conclude(
        self,
        work: ClaimedReasonRun,
        executor: ReasonExecutor,
        execution_input: ExecutionInput,
        workspace: ReasonWorkspace,
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
            retry_errors=(ReasonPlanError, StageOutputError),
            default_timeout_seconds=120,
            progress=make_closeout_progress(
                self.control_plane.activity,
                work,
                verb="Reason 收口整理中：补齐 Workspace 校验缺口",
            ),
        )

    def _validate_finish_and_workspace(
        self,
        work: ClaimedReasonRun,
        workspace: ReasonWorkspace,
        raw_output: object,
    ) -> None:
        require_finish(str(raw_output))
        status = self.control_plane.reason_workspace_status(
            work,
            workspace_id=workspace.id,
        )
        if not status.ready:
            raise ReasonPlanError("；".join(status.errors) or "Reason Workspace 未就绪")

    def _close_failed_attempt(
        self,
        work: ClaimedReasonRun,
        workspace: ReasonWorkspace | None,
        *,
        failure_kind: str,
        failure_class: str,
        keep_workspace: bool = False,
    ) -> None:
        if workspace is not None and not keep_workspace:
            try:
                self.control_plane.abandon_reason_workspace(
                    work,
                    workspace_id=workspace.id,
                    failure_reason=failure_kind,
                )
            except LeaseLostError:
                pass
            except BaseException:
                logger.exception("failed to abandon Reason Workspace %s", workspace.id)
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
                "failed to release ReasonRun %s/%s after %s",
                work.task_id,
                work.reason_run_id,
                failure_kind,
            )
