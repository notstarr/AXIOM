from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Protocol

from axiom.dispatcher.closeout import bounded_closeout, make_closeout_progress
from axiom.dispatcher.heartbeat import LeaseHeartbeat, LeaseLostError
from axiom.dispatcher.http import ControlPlaneError, is_transient_control_plane_error
from axiom.dispatcher.schemas import ClaimedIntent
from axiom.modules.execution.schemas import (
    BootstrapCompleted,
    CompleteBootstrapCommand,
    ExecutionInput,
    ReleaseIntentCommand,
)
from axiom.modules.execution.stage_outputs import (
    StageOutputError,
    bootstrap_files,
    require_finish,
)


logger = logging.getLogger(__name__)


class BootstrapExecutionTimeout(TimeoutError):
    """Execute budget expired but collected evidence may still be concluded."""


class TransientWorkerError(RuntimeError):
    """Provider/process infrastructure failed before a valid result existed."""


class AxiomToolLoadError(TransientWorkerError):
    """A selected AXIOM Tool package failed before the model could run."""


class BootstrapExecutor(Protocol):
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


class BootstrapControlPlane(Protocol):
    def start(self, work: ClaimedIntent) -> None:
        ...

    def heartbeat(self, work: ClaimedIntent) -> None:
        ...

    def input(self, work: ClaimedIntent) -> ExecutionInput:
        ...

    def begin_conclude(self, work: ClaimedIntent) -> None:
        ...

    def complete_bootstrap(
        self,
        work: ClaimedIntent,
        command: CompleteBootstrapCommand,
    ) -> BootstrapCompleted:
        ...

    def release(self, work: ClaimedIntent, command: ReleaseIntentCommand) -> None:
        ...


class BootstrapRunner:
    """Execute once, then perform bounded same-session closeout if needed."""

    def __init__(
        self,
        control_plane: BootstrapControlPlane,
        *,
        heartbeat_interval_seconds: float = 10.0,
        cancellation: threading.Event | None = None,
    ):
        self.control_plane = control_plane
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.cancellation = cancellation

    def run(
        self,
        work: ClaimedIntent,
        executor: BootstrapExecutor,
    ) -> BootstrapCompleted:
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
                self._validate_output(execution_input, raw_output)
            except BootstrapExecutionTimeout:
                self._conclude(
                    work,
                    executor,
                    execution_input,
                    heartbeat,
                    reason="execute_timeout",
                )
            except StageOutputError as exc:
                self._conclude(
                    work,
                    executor,
                    execution_input,
                    heartbeat,
                    reason=str(exc),
                )

            heartbeat.ensure_active()
            return self.control_plane.complete_bootstrap(
                work,
                CompleteBootstrapCommand(lease_id=work.lease_id),
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
        except ControlPlaneError as exc:
            transient = is_transient_control_plane_error(exc)
            self._release_best_effort(
                work,
                failure_kind="control_plane_unavailable" if transient else "bootstrap_execution_failed",
                failure_class="transient" if transient else "execution",
            )
            raise
        except BaseException:
            self._release_best_effort(
                work,
                failure_kind="bootstrap_execution_failed",
                failure_class="execution",
            )
            raise
        finally:
            heartbeat.stop()

    def _conclude(
        self,
        work: ClaimedIntent,
        executor: BootstrapExecutor,
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
            validate=self._validate_output,
            reason=reason,
            retry_errors=(StageOutputError,),
            default_timeout_seconds=120,
            progress=make_closeout_progress(
                self.control_plane.activity,
                work,
                verb="Bootstrap 收口整理中：补齐产物并完成校验",
            ),
        )

    @staticmethod
    def _validate_output(execution_input: ExecutionInput, raw_output: object) -> None:
        require_finish(str(raw_output))
        bootstrap_files(execution_input.run_dir)

    def _release_best_effort(
        self,
        work: ClaimedIntent,
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
                "failed to release Intent %s/%s after %s",
                work.task_id,
                work.intent_id,
                failure_kind,
            )
