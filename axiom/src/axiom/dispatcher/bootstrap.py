from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Protocol

from pydantic import BaseModel, ValidationError, field_validator

from axiom.dispatcher.heartbeat import LeaseHeartbeat, LeaseLostError
from axiom.dispatcher.schemas import ClaimedIntent
from axiom.modules.artifacts.schemas import BootstrapBundle
from axiom.modules.execution.schemas import (
    BootstrapCompleted,
    CompleteBootstrapCommand,
    ExecutionInput,
    ReleaseIntentCommand,
)


logger = logging.getLogger(__name__)


class BootstrapExecutionTimeout(TimeoutError):
    """Execute budget expired but collected evidence may still be concluded."""


class TransientWorkerError(RuntimeError):
    """Provider/process infrastructure failed before a valid result existed."""


class BootstrapWorkerOutput(BaseModel):
    summary: str
    artifacts: BootstrapBundle

    @field_validator("summary")
    @classmethod
    def require_summary(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Bootstrap summary must not be empty")
        return value


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
    """Execute exactly once, conclude at most once, then close atomically."""

    def __init__(
        self,
        control_plane: BootstrapControlPlane,
        *,
        heartbeat_interval_seconds: float = 10.0,
    ):
        self.control_plane = control_plane
        self.heartbeat_interval_seconds = heartbeat_interval_seconds

    def run(
        self,
        work: ClaimedIntent,
        executor: BootstrapExecutor,
    ) -> BootstrapCompleted:
        heartbeat = LeaseHeartbeat(
            lambda: self.control_plane.heartbeat(work),
            interval_seconds=self.heartbeat_interval_seconds,
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
                output = BootstrapWorkerOutput.model_validate(raw_output)
            except BootstrapExecutionTimeout:
                output = self._conclude(
                    work,
                    executor,
                    execution_input,
                    heartbeat,
                    reason="execute_timeout",
                )
            except ValidationError:
                output = self._conclude(
                    work,
                    executor,
                    execution_input,
                    heartbeat,
                    reason="invalid_execute_output",
                )

            heartbeat.ensure_active()
            return self.control_plane.complete_bootstrap(
                work,
                CompleteBootstrapCommand(
                    lease_id=work.lease_id,
                    summary=output.summary,
                    artifacts=output.artifacts,
                ),
            )
        except LeaseLostError:
            self._release_best_effort(
                work,
                failure_kind="lease_heartbeat_lost",
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
    ) -> BootstrapWorkerOutput:
        heartbeat.ensure_active()
        self.control_plane.begin_conclude(work)
        raw_output = executor.conclude(
            execution_input,
            reason=reason,
            lease_active=heartbeat.is_active,
        )
        return BootstrapWorkerOutput.model_validate(raw_output)

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
