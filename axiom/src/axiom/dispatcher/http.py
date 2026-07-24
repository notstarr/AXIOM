from __future__ import annotations

from typing import Any

import httpx

from axiom.dispatcher.heartbeat import LeaseLostError
from axiom.dispatcher.schemas import ClaimedIntent, DispatcherSnapshot
from axiom.modules.execution.schemas import (
    ActivityCommand,
    BootstrapCompleted,
    CompleteBootstrapCommand,
    CompleteExploreCommand,
    ExecutionInput,
    ExploreCompleted,
    ReleaseIntentCommand,
    StartedIntent,
)
from axiom.modules.dispatch.schemas import RecoveryReport
from axiom.modules.reason.schemas import CommitResult, ReasonWorkspace, WorkspaceDraft
from axiom.modules.settings.schemas import RuntimeSettings
from axiom.modules.workers.schemas import Worker


class ControlPlaneError(RuntimeError):
    def __init__(self, message: str, *, status_code: int, code: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


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
        self.base_url = base_url.rstrip("/") + "/api/v1"
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

    def runtime_settings(self) -> RuntimeSettings:
        return RuntimeSettings.model_validate(self._request("GET", "/settings/runtime"))

    def claim(
        self,
        *,
        task_id: str,
        intent_id: str,
        worker_name: str,
    ) -> ClaimedIntent | None:
        try:
            payload = self._request(
                "POST",
                f"/dispatch/tasks/{task_id}/intents/{intent_id}/claim",
                {"worker_name": worker_name},
            )
        except ControlPlaneError as exc:
            if exc.status_code == 409:
                return None
            raise
        return ClaimedIntent.model_validate(payload)

    def recover_stale(self, *, stale_after_seconds: int) -> RecoveryReport:
        return RecoveryReport.model_validate(
            self._request(
                "POST",
                "/dispatch/recover-stale",
                {"stale_after_seconds": stale_after_seconds},
            )
        )

    def start(self, work: ClaimedIntent) -> StartedIntent:
        return StartedIntent.model_validate(
            self._request("POST", self._work_path("start", work), self._lease(work))
        )

    def heartbeat(self, work: ClaimedIntent) -> None:
        try:
            self._request("POST", self._work_path("heartbeat", work), self._lease(work))
        except ControlPlaneError as exc:
            if exc.code == "lease_inactive":
                raise LeaseLostError(str(exc)) from exc
            raise

    def activity(self, work: ClaimedIntent, command: ActivityCommand) -> None:
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

    def input(self, work: ClaimedIntent) -> ExecutionInput:
        return ExecutionInput.model_validate(
            self._request("POST", self._work_path("input", work), self._lease(work))
        )

    def begin_conclude(self, work: ClaimedIntent) -> None:
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

    def release(self, work: ClaimedIntent, command: ReleaseIntentCommand) -> None:
        try:
            self._request(
                "POST",
                self._work_path("release", work),
                command.model_dump(mode="json"),
            )
        except ControlPlaneError as exc:
            # Release is best-effort and idempotent from the Runner's point of
            # view.  A stopped Task or superseded lease already has the desired
            # outcome: this process no longer owns executable work.
            if exc.code != "lease_inactive":
                raise

    def open_reason_workspace(
        self,
        work: ClaimedIntent,
        *,
        context_revision: int,
    ) -> ReasonWorkspace:
        return ReasonWorkspace.model_validate(
            self._request(
                "POST",
                f"/tasks/{work.task_id}/reason/workspaces",
                {
                    "intent_id": work.intent_id,
                    "lease_id": work.lease_id,
                    "context_revision": context_revision,
                },
            )
        )

    def submit_reason_workspace(
        self,
        work: ClaimedIntent,
        *,
        workspace_id: str,
        draft: WorkspaceDraft,
    ) -> CommitResult:
        try:
            payload = self._request(
                "POST",
                f"/reason-workspaces/{workspace_id}/submit",
                {"lease_id": work.lease_id, "draft": draft.model_dump(mode="json")},
            )
        except ControlPlaneError as exc:
            if exc.code == "lease_inactive":
                raise LeaseLostError(str(exc)) from exc
            raise
        return CommitResult.model_validate(payload)

    def abandon_reason_workspace(
        self,
        work: ClaimedIntent,
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

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        try:
            response = self._client.request(method, path, json=body)
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
    def _work_path(action: str, work: ClaimedIntent) -> str:
        return f"/dispatch/tasks/{work.task_id}/intents/{work.intent_id}/{action}"

    @staticmethod
    def _lease(work: ClaimedIntent) -> dict[str, str]:
        return {"lease_id": work.lease_id}
