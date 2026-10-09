from __future__ import annotations

import logging
from typing import Any

import httpx

from axiom.dispatcher.heartbeat import LeaseLostError
from axiom.dispatcher.schemas import (
    ClaimAction,
    ClaimedIntent,
    ClaimedPlanRun,
    ClaimedWork,
    DispatcherSnapshot,
)
from axiom.modules.dispatch.schemas import RecoveryReport
from axiom.modules.execution.schemas import (
    ActivityCommand,
    BootstrapCompleted,
    CompleteBootstrapCommand,
    CompleteExploreCommand,
    ExecutionInput,
    PlanExecutionInput,
    ExploreCompleted,
    ReleaseIntentCommand,
    StartedIntent,
)
from axiom.modules.reason.schemas import (
    ClaimedReasonRun,
    CommitResult,
    ReasonReconcileReport,
    ReasonWorkspace,
    ReleasedReasonRun,
    StartedReasonRun,
    WorkspaceStatus,
)
from axiom.modules.plan.schemas import (
    PlanCommitResult,
    PlanReconcileReport,
    PlanWorkspace,
    PlanWorkspaceStatus,
    ReleasedPlanRun,
    StartedPlanRun,
)
from axiom.modules.settings.schemas import RuntimeSettings
from axiom.modules.tasks.schemas import RunDeadlineReconcileReport
from axiom.modules.token_usage.schemas import TokenUsageBatchCommand, TokenUsageBatchResult
from axiom.modules.interventions.schemas import Intervention, ResolveIntervention
from axiom.modules.vulnerability_reviews.schemas import (
    ClaimedVulnerabilityReviewRun,
    CompleteReviewCommand,
    ReleasedVulnerabilityReviewRun,
    ReviewStatus,
    StartedVulnerabilityReviewRun,
    VulnerabilityReviewCompleted,
)
from axiom.modules.workers.schemas import Worker


logger = logging.getLogger(__name__)


CONTROL_PLANE_INPUT_TIMEOUT_SECONDS = 120.0


class ControlPlaneError(RuntimeError):
    def __init__(self, message: str, *, status_code: int, code: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def is_transient_control_plane_error(exc: ControlPlaneError) -> bool:
    """Return whether a control-plane failure is safe to retry as infrastructure."""

    return exc.status_code == 0 or exc.status_code in {408, 429} or exc.status_code >= 500


class HttpControlPlane:
    """HTTP-only dispatcher adapter; it never opens the AXIOM database."""

    def __init__(
        self,
        base_url: str,
        *,
        service_token: str = "",
        timeout_seconds: float = 20.0,
        client: httpx.Client | None = None,
    ):
        self.api_url = base_url.rstrip("/")
        self.base_url = self.api_url + "/api/v1"
        self.service_token = service_token
        self._headers = {"content-type": "application/json"}
        if service_token:
            self._headers["authorization"] = f"Bearer {service_token}"
        self._client = client or httpx.Client(
            base_url=self.base_url,
            headers=self._headers,
            timeout=timeout_seconds,
            trust_env=False,
        )
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def snapshot(self) -> DispatcherSnapshot:
        return DispatcherSnapshot.model_validate(self._request("GET", "/dispatch/snapshot"))

    def worker(self, name: str) -> Worker:
        return Worker.model_validate(self._request("GET", f"/workers/{name}"))

    def set_worker_capability(self, name: str, developer_role_supported: bool | None) -> None:
        """Persist a probed gateway capability for a Worker."""

        self._request(
            "POST",
            f"/workers/{name}/capability",
            {"developer_role_supported": developer_role_supported},
        )

    def runtime_settings(self) -> RuntimeSettings:
        return RuntimeSettings.model_validate(self._request("GET", "/settings/runtime"))

    def mark_runtime_settings_applied(self, version: int) -> RuntimeSettings:
        return RuntimeSettings.model_validate(
            self._request("POST", "/settings/runtime/applied", {"version": version})
        )

    def reconcile_reason_runs(self) -> ReasonReconcileReport:
        return ReasonReconcileReport.model_validate(
            self._request("POST", "/dispatch/reason-runs/reconcile", {})
        )

    def reconcile_plan_runs(self) -> PlanReconcileReport:
        return PlanReconcileReport.model_validate(
            self._request("POST", "/dispatch/plan-runs/reconcile", {})
        )

    def reconcile_task_deadlines(self) -> RunDeadlineReconcileReport:
        return RunDeadlineReconcileReport.model_validate(
            self._request("POST", "/dispatch/task-deadlines/reconcile", {})
        )

    def claim_work(self, action: ClaimAction) -> ClaimedWork | None:
        if action.intent_id:
            path = f"/dispatch/tasks/{action.task_id}/intents/{action.intent_id}/claim"
            model = ClaimedIntent
        elif action.reason_run_id:
            path = (
                f"/dispatch/tasks/{action.task_id}/reason-runs/"
                f"{action.reason_run_id}/claim"
            )
            model = ClaimedReasonRun
        elif action.plan_run_id:
            path = (
                f"/dispatch/tasks/{action.task_id}/plan-runs/"
                f"{action.plan_run_id}/claim"
            )
            model = ClaimedPlanRun
        else:
            assert action.review_run_id is not None
            path = (
                f"/dispatch/tasks/{action.task_id}/vulnerability-review-runs/"
                f"{action.review_run_id}/claim"
            )
            model = ClaimedVulnerabilityReviewRun
        try:
            payload = self._request("POST", path, {"worker_name": action.worker_name})
        except ControlPlaneError as exc:
            if exc.status_code == 409:
                return None
            raise
        return model.model_validate(payload)

    def claim(
        self,
        *,
        task_id: str,
        intent_id: str,
        worker_name: str,
    ) -> ClaimedIntent | None:
        result = self.claim_work(
            ClaimAction(
                task_id=task_id,
                intent_id=intent_id,
                worker_name=worker_name,
                kind="bootstrap",
            )
        )
        return result if isinstance(result, ClaimedIntent) else None

    def recover_stale(self, *, stale_after_seconds: int) -> RecoveryReport:
        return RecoveryReport.model_validate(
            self._request(
                "POST",
                "/dispatch/recover-stale",
                {"stale_after_seconds": stale_after_seconds},
            )
        )

    def start(
        self, work: ClaimedWork
    ) -> StartedIntent | StartedReasonRun | StartedPlanRun | StartedVulnerabilityReviewRun:
        payload = self._request("POST", self._work_path("start", work), self._lease(work))
        if isinstance(work, ClaimedReasonRun):
            return StartedReasonRun.model_validate(payload)
        if isinstance(work, ClaimedPlanRun):
            return StartedPlanRun.model_validate(payload)
        if isinstance(work, ClaimedVulnerabilityReviewRun):
            return StartedVulnerabilityReviewRun.model_validate(payload)
        return StartedIntent.model_validate(payload)

    def heartbeat(self, work: ClaimedWork) -> None:
        try:
            self._request("POST", self._work_path("heartbeat", work), self._lease(work))
        except ControlPlaneError as exc:
            if exc.code == "lease_inactive":
                raise LeaseLostError(str(exc)) from exc
            raise

    def activity(self, work: ClaimedWork, command: ActivityCommand) -> None:
        try:
            self._request(
                "POST",
                self._work_path("activity", work),
                command.model_dump(mode="json"),
            )
        except ControlPlaneError as exc:
            if exc.code == "lease_inactive":
                raise LeaseLostError(str(exc)) from exc
            raise

    def token_usage(
        self,
        work: ClaimedWork,
        command: TokenUsageBatchCommand,
    ) -> TokenUsageBatchResult:
        return TokenUsageBatchResult.model_validate(
            self._request(
                "POST",
                self._work_path("token-usage", work),
                command.model_dump(mode="json"),
            )
        )

    def claim_interventions(self, work: ClaimedWork) -> list[Intervention]:
        if not isinstance(work, ClaimedIntent) or work.kind != "explore":
            return []
        try:
            payload = self._request(
                "POST",
                f"/dispatch/tasks/{work.task_id}/intents/{work.intent_id}/interventions/claim",
                self._lease(work),
            )
        except ControlPlaneError as exc:
            if exc.code == "lease_inactive":
                raise LeaseLostError(str(exc)) from exc
            raise
        return [Intervention.model_validate(item) for item in payload or []]

    def resolve_intervention(
        self,
        work: ClaimedWork,
        intervention_id: str,
        *,
        success: bool,
        error: str = "",
    ) -> Intervention:
        assert isinstance(work, ClaimedIntent) and work.intent_id
        return Intervention.model_validate(
            self._request(
                "POST",
                f"/dispatch/tasks/{work.task_id}/intents/{work.intent_id}/interventions/{intervention_id}/resolve"
                f"?lease_id={work.lease_id}",
                ResolveIntervention(success=success, error=error).model_dump(mode="json"),
            )
        )

    def expire_interventions(self, work: ClaimedWork) -> None:
        if not isinstance(work, ClaimedIntent) or work.kind != "explore":
            return
        try:
            self._request(
                "POST",
                f"/dispatch/tasks/{work.task_id}/intents/{work.intent_id}/interventions/expire",
                self._lease(work),
            )
        except ControlPlaneError:
            logger.warning("failed to expire Explore interventions for %s/%s", work.task_id, work.intent_id, exc_info=True)

    def input(self, work: ClaimedWork) -> ExecutionInput | PlanExecutionInput:
        payload = self._request(
            "POST",
            self._work_path("input", work),
            self._lease(work),
            timeout_seconds=CONTROL_PLANE_INPUT_TIMEOUT_SECONDS,
        )
        if isinstance(work, ClaimedPlanRun):
            return PlanExecutionInput.model_validate(payload)
        return ExecutionInput.model_validate(payload)

    def begin_conclude(self, work: ClaimedWork) -> None:
        self._request("POST", self._work_path("conclude", work), self._lease(work))

    def complete_bootstrap(
        self,
        work: ClaimedIntent,
        command: CompleteBootstrapCommand,
    ) -> BootstrapCompleted:
        return BootstrapCompleted.model_validate(
            self._request(
                "POST",
                self._work_path("bootstrap-complete", work),
                command.model_dump(mode="json"),
            )
        )

    def complete_explore(
        self,
        work: ClaimedIntent,
        command: CompleteExploreCommand,
    ) -> ExploreCompleted:
        return ExploreCompleted.model_validate(
            self._request(
                "POST",
                self._work_path("explore-complete", work),
                command.model_dump(mode="json"),
            )
        )

    def complete_review(
        self,
        work: ClaimedVulnerabilityReviewRun,
        command: CompleteReviewCommand,
    ) -> VulnerabilityReviewCompleted:
        return VulnerabilityReviewCompleted.model_validate(
            self._request(
                "POST",
                self._work_path("complete", work),
                command.model_dump(mode="json"),
            )
        )

    def review_status(
        self,
        work: ClaimedVulnerabilityReviewRun,
    ) -> ReviewStatus:
        try:
            payload = self._request(
                "POST",
                self._work_path("status", work),
                self._lease(work),
            )
        except ControlPlaneError as exc:
            if exc.code == "lease_inactive":
                raise LeaseLostError(str(exc)) from exc
            raise
        return ReviewStatus.model_validate(payload)

    def release(
        self,
        work: ClaimedWork,
        command: ReleaseIntentCommand,
    ) -> ReleasedReasonRun | ReleasedPlanRun | ReleasedVulnerabilityReviewRun | None:
        try:
            payload = self._request(
                "POST",
                self._work_path("release", work),
                command.model_dump(mode="json"),
            )
        except ControlPlaneError as exc:
            if exc.code != "lease_inactive":
                raise
            return None
        if isinstance(work, ClaimedReasonRun):
            return ReleasedReasonRun.model_validate(payload)
        if isinstance(work, ClaimedPlanRun):
            return ReleasedPlanRun.model_validate(payload)
        if isinstance(work, ClaimedVulnerabilityReviewRun):
            return ReleasedVulnerabilityReviewRun.model_validate(payload)
        return None

    def open_reason_workspace(
        self,
        work: ClaimedReasonRun,
        *,
        context_revision: int,
    ) -> ReasonWorkspace:
        return ReasonWorkspace.model_validate(
            self._request(
                "POST",
                f"/tasks/{work.task_id}/reason/workspaces",
                {
                    "reason_run_id": work.reason_run_id,
                    "lease_id": work.lease_id,
                    "context_revision": context_revision,
                },
            )
        )

    def open_plan_workspace(
        self,
        work: ClaimedPlanRun,
        *,
        context_revision: int,
    ) -> PlanWorkspace:
        return PlanWorkspace.model_validate(
            self._request(
                "POST",
                f"/tasks/{work.task_id}/plan/workspaces",
                {
                    "plan_run_id": work.plan_run_id,
                    "lease_id": work.lease_id,
                    "context_revision": context_revision,
                },
            )
        )

    def plan_workspace_status(
        self,
        work: ClaimedPlanRun,
        *,
        workspace_id: str,
    ) -> PlanWorkspaceStatus:
        try:
            payload = self._request(
                "POST",
                f"/plan-workspaces/{workspace_id}/status",
                self._lease(work),
            )
        except ControlPlaneError as exc:
            if exc.code == "lease_inactive":
                raise LeaseLostError(str(exc)) from exc
            raise
        return PlanWorkspaceStatus.model_validate(payload)

    def commit_plan_workspace(
        self,
        work: ClaimedPlanRun,
        *,
        workspace_id: str,
    ) -> PlanCommitResult:
        try:
            payload = self._request(
                "POST",
                f"/plan-workspaces/{workspace_id}/commit",
                self._lease(work),
            )
        except ControlPlaneError as exc:
            if exc.code == "lease_inactive":
                raise LeaseLostError(str(exc)) from exc
            raise
        return PlanCommitResult.model_validate(payload)

    def abandon_plan_workspace(
        self,
        work: ClaimedPlanRun,
        *,
        workspace_id: str,
        failure_reason: str,
    ) -> None:
        try:
            self._request(
                "POST",
                f"/plan-workspaces/{workspace_id}/abandon",
                {"lease_id": work.lease_id, "failure_reason": failure_reason},
            )
        except ControlPlaneError as exc:
            if exc.code == "lease_inactive":
                raise LeaseLostError(str(exc)) from exc
            raise

    def reason_workspace_status(
        self,
        work: ClaimedReasonRun,
        *,
        workspace_id: str,
    ) -> WorkspaceStatus:
        try:
            payload = self._request(
                "POST",
                f"/reason-workspaces/{workspace_id}/status",
                self._lease(work),
            )
        except ControlPlaneError as exc:
            if exc.code == "lease_inactive":
                raise LeaseLostError(str(exc)) from exc
            raise
        return WorkspaceStatus.model_validate(payload)

    def commit_reason_workspace(
        self,
        work: ClaimedReasonRun,
        *,
        workspace_id: str,
    ) -> CommitResult:
        try:
            payload = self._request(
                "POST",
                f"/reason-workspaces/{workspace_id}/commit",
                self._lease(work),
            )
        except ControlPlaneError as exc:
            if exc.code == "lease_inactive":
                raise LeaseLostError(str(exc)) from exc
            raise
        return CommitResult.model_validate(payload)

    def abandon_reason_workspace(
        self,
        work: ClaimedReasonRun,
        *,
        workspace_id: str,
        failure_reason: str,
    ) -> None:
        try:
            self._request(
                "POST",
                f"/reason-workspaces/{workspace_id}/abandon",
                {"lease_id": work.lease_id, "failure_reason": failure_reason},
            )
        except ControlPlaneError as exc:
            if exc.code == "lease_inactive":
                raise LeaseLostError(str(exc)) from exc
            raise

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> Any:
        try:
            request_kwargs: dict[str, Any] = {"json": body}
            if timeout_seconds is not None:
                request_kwargs["timeout"] = timeout_seconds
            response = self._client.request(method, path, **request_kwargs)
        except httpx.HTTPError as exc:
            raise ControlPlaneError(str(exc), status_code=0) from exc
        if response.is_error:
            try:
                error = response.json().get("error", {})
                detail = error.get("message", response.text)
                code = error.get("code", "")
            except (ValueError, AttributeError):
                detail = response.text
                code = ""
            raise ControlPlaneError(
                f"HTTP {response.status_code}: {detail}",
                status_code=response.status_code,
                code=code,
            )
        if not response.content:
            return None
        return response.json()

    @staticmethod
    def _work_path(action: str, work: ClaimedWork) -> str:
        if isinstance(work, ClaimedReasonRun):
            return (
                f"/dispatch/tasks/{work.task_id}/reason-runs/"
                f"{work.reason_run_id}/{action}"
            )
        if isinstance(work, ClaimedPlanRun):
            return (
                f"/dispatch/tasks/{work.task_id}/plan-runs/"
                f"{work.plan_run_id}/{action}"
            )
        if isinstance(work, ClaimedVulnerabilityReviewRun):
            return (
                f"/dispatch/tasks/{work.task_id}/vulnerability-review-runs/"
                f"{work.review_run_id}/{action}"
            )
        return f"/dispatch/tasks/{work.task_id}/intents/{work.intent_id}/{action}"

    @staticmethod
    def _lease(work: ClaimedWork) -> dict[str, str]:
        return {"lease_id": work.lease_id}
