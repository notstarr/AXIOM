from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Protocol

from axiom.dispatcher.bootstrap import AxiomToolLoadError, TransientWorkerError
from axiom.dispatcher.closeout import bounded_closeout, make_closeout_progress
from axiom.dispatcher.heartbeat import LeaseHeartbeat, LeaseLostError
from axiom.dispatcher.http import ControlPlaneError, is_transient_control_plane_error
from axiom.dispatcher.schemas import ClaimedIntent
from axiom.modules.execution.schemas import (
    CompleteExploreCommand,
    ExecutionInput,
    ExploreCompleted,
    ReleaseIntentCommand,
)
from axiom.modules.execution.stage_outputs import (
    StageOutputError,
    explore_files,
    initialize_explore_state,
    require_explore_finish,
)


logger = logging.getLogger(__name__)


class ExploreExecutionTimeout(TimeoutError):
    """Explore budget expired; partial evidence may still be concluded."""


class ExploreExecutor(Protocol):
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


class ExploreControlPlane(Protocol):
    def start(self, work: ClaimedIntent) -> None:
        ...

    def heartbeat(self, work: ClaimedIntent) -> None:
        ...

    def input(self, work: ClaimedIntent) -> ExecutionInput:
        ...

    def begin_conclude(self, work: ClaimedIntent) -> None:
        ...

    def complete_explore(
        self,
        work: ClaimedIntent,
        command: CompleteExploreCommand,
    ) -> ExploreCompleted:
        ...

    def release(self, work: ClaimedIntent, command: ReleaseIntentCommand) -> None:
        ...

    def expire_interventions(self, work: ClaimedIntent) -> None:
        ...


class ExploreRunner:
    """Execute once, then perform bounded closeout and atomically persist evidence."""

    def __init__(
        self,
        control_plane: ExploreControlPlane,
        *,
        heartbeat_interval_seconds: float = 10.0,
        cancellation: threading.Event | None = None,
    ):
        self.control_plane = control_plane
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.cancellation = cancellation

    def run(self, work: ClaimedIntent, executor: ExploreExecutor) -> ExploreCompleted:
        heartbeat = LeaseHeartbeat(
            lambda: self.control_plane.heartbeat(work),
            interval_seconds=self.heartbeat_interval_seconds,
            cancellation=self.cancellation,
        )
        try:
            self.control_plane.start(work)
            heartbeat.start()
            execution_input = self.control_plane.input(work)
            initialize_explore_state(execution_input.run_dir)
            try:
                raw_output = executor.execute(
                    execution_input,
                    lease_active=heartbeat.is_active,
                )
                self._validate_output(execution_input, raw_output)
            except ExploreExecutionTimeout:
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
            return self.control_plane.complete_explore(
                work,
                CompleteExploreCommand(lease_id=work.lease_id),
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
                failure_kind="control_plane_unavailable" if transient else "explore_execution_failed",
                failure_class="transient" if transient else "execution",
            )
            raise
        except BaseException:
            self._release_best_effort(
                work,
                failure_kind="explore_execution_failed",
                failure_class="execution",
            )
            raise
        finally:
            expire = getattr(self.control_plane, "expire_interventions", None)
            if expire is not None:
                try:
                    expire(work)
                except Exception:
                    logger.warning("failed to expire human Explore interventions", exc_info=True)
            heartbeat.stop()

    def _conclude(
        self,
        work: ClaimedIntent,
        executor: ExploreExecutor,
        execution_input: ExecutionInput,
        heartbeat: LeaseHeartbeat,
        *,
        reason: str,
    ) -> None:
        heartbeat.ensure_active()
        try:
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
                default_timeout_seconds=180,
                progress=make_closeout_progress(
                    self.control_plane.activity,
                    work,
                    verb="收口整理中：补齐阶段产物并完成校验",
                ),
            )
        except (StageOutputError, TimeoutError) as exc:
            heartbeat.ensure_active()
            try:
                outputs = explore_files(execution_input.run_dir, emit_warnings=False)
            except StageOutputError as core_error:
                raise core_error from exc
            evidence_count = sum(
                item.artifact_type == "explore.evidence" for item in outputs
            )
            logger.warning(
                "explore_closeout_core_fallback task_id=%s intent_id=%s lease_id=%s "
                "original_reason=%r closeout_error=%r finish_confirmed=false "
                "evidence_count=%s",
                execution_input.task_id,
                execution_input.intent_id,
                execution_input.lease_id,
                reason,
                str(exc),
                evidence_count,
            )

    @staticmethod
    def _validate_output(execution_input: ExecutionInput, raw_output: object) -> None:
        normalization = require_explore_finish(str(raw_output))
        # Validation may run repeatedly during bounded closeout. Defer optional
        # Artifact warnings to the single authoritative completion scan so a
        # skipped Evidence file does not produce duplicate warnings per attempt.
        explore_files(execution_input.run_dir, emit_warnings=False)
        if normalization is not None:
            logger.warning(
                "explore_finish_normalized task_id=%s intent_id=%s lease_id=%s "
                "normalization=%s",
                execution_input.task_id,
                execution_input.intent_id,
                execution_input.lease_id,
                normalization,
            )

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
                "failed to release Explore Intent %s/%s after %s",
                work.task_id,
                work.intent_id,
                failure_kind,
            )
