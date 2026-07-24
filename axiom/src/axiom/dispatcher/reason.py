from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Protocol

from pydantic import ValidationError

from axiom.dispatcher.bootstrap import TransientWorkerError
from axiom.dispatcher.heartbeat import LeaseHeartbeat, LeaseLostError
from axiom.dispatcher.schemas import ClaimedIntent
from axiom.modules.execution.schemas import ExecutionInput, ReleaseIntentCommand
from axiom.modules.reason.schemas import CommitResult, ReasonWorkspace, WorkspaceDraft


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
        lease_active: Callable[[], bool],
    ) -> object:
        ...


class ReasonControlPlane(Protocol):
    def start(self, work: ClaimedIntent) -> None:
        ...

    def heartbeat(self, work: ClaimedIntent) -> None:
        ...

    def input(self, work: ClaimedIntent) -> ExecutionInput:
        ...

    def open_reason_workspace(
        self,
        work: ClaimedIntent,
        *,
        context_revision: int,
    ) -> ReasonWorkspace:
        ...

    def submit_reason_workspace(
        self,
        work: ClaimedIntent,
        *,
        workspace_id: str,
        draft: WorkspaceDraft,
    ) -> CommitResult:
        ...

    def abandon_reason_workspace(
        self,
        work: ClaimedIntent,
        *,
        workspace_id: str,
        failure_reason: str,
    ) -> None:
        ...

    def release(self, work: ClaimedIntent, command: ReleaseIntentCommand) -> None:
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

    def run(self, work: ClaimedIntent, executor: ReasonExecutor) -> CommitResult:
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
            raw_output = executor.execute(
                execution_input,
                lease_active=heartbeat.is_active,
            )
            draft = WorkspaceDraft.model_validate(raw_output)
            heartbeat.ensure_active()
            return self.control_plane.submit_reason_workspace(
                work,
                workspace_id=workspace.id,
                draft=draft,
            )
        except LeaseLostError:
            self._close_failed_attempt(
                work,
                workspace,
                failure_kind="lease_heartbeat_lost",
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
        except (ReasonExecutionTimeout, ReasonPlanError, ValidationError):
            self._close_failed_attempt(
                work,
                workspace,
                failure_kind="reason_plan_invalid",
                failure_class="execution",
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

    def _close_failed_attempt(
        self,
        work: ClaimedIntent,
        workspace: ReasonWorkspace | None,
        *,
        failure_kind: str,
        failure_class: str,
    ) -> None:
        if workspace is not None:
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
                "failed to release Reason Intent %s/%s after %s",
                work.task_id,
                work.intent_id,
                failure_kind,
            )
