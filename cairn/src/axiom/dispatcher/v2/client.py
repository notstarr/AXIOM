from __future__ import annotations

import threading
from typing import Any

import requests
from requests.adapters import HTTPAdapter

from axiom.dispatcher.v2.schemas import DispatcherSnapshot
from axiom.modules.graph.schemas import Intent
from axiom.modules.reason_workspace.schemas import ReasonWorkspace
from axiom.modules.settings.schemas import RuntimeSettings
from axiom.modules.tasks.schemas import Task


class V2ProtocolError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 0):
        super().__init__(message)
        self.status_code = status_code


class V2Client:
    def __init__(self, base_url: str, *, service_token: str, timeout: float = 20):
        self.base_url = base_url.rstrip("/")
        self.service_token = service_token
        self.timeout = timeout
        self._local = threading.local()
        self._sessions: list[requests.Session] = []
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            sessions, self._sessions = self._sessions, []
        for session in sessions:
            session.close()

    def snapshot(self) -> DispatcherSnapshot:
        return DispatcherSnapshot.model_validate(self._request("GET", "/internal/dispatcher/snapshot"))

    def recover_stale_leases(self, timeout_seconds: int) -> dict[str, Any]:
        return self._request(
            "POST",
            "/internal/dispatcher/recover-stale-leases",
            {"timeout_seconds": timeout_seconds},
        )

    def runtime_settings(self) -> RuntimeSettings:
        return RuntimeSettings.model_validate(self._request("GET", "/settings/runtime", internal=False))

    def task(self, task_id: str) -> Task:
        return Task.model_validate(self._request("GET", f"/tasks/{task_id}", internal=False))

    def intents(self, task_id: str) -> list[Intent]:
        return [Intent.model_validate(item) for item in self._request("GET", f"/tasks/{task_id}/intents", internal=False)]

    def build_context(self, task_id: str, attempt_id: str) -> dict[str, Any]:
        return self._request("POST", f"/internal/tasks/{task_id}/context", {"attempt_id": attempt_id})

    def claim_intent(self, task_id: str, intent_id: str, worker: str) -> Intent:
        return Intent.model_validate(
            self._request("POST", f"/internal/tasks/{task_id}/intents/{intent_id}/claim", {"worker": worker})
        )

    def heartbeat_intent(self, task_id: str, intent_id: str, worker: str) -> Intent:
        return Intent.model_validate(
            self._request("POST", f"/internal/tasks/{task_id}/intents/{intent_id}/heartbeat", {"worker": worker})
        )

    def release_intent(
        self,
        task_id: str,
        intent_id: str,
        worker: str,
        *,
        failure_kind: str,
        next_retry_at: str | None,
    ) -> Intent:
        return Intent.model_validate(
            self._request(
                "POST",
                f"/internal/tasks/{task_id}/intents/{intent_id}/release",
                {
                    "worker": worker,
                    "failure_kind": failure_kind,
                    "next_retry_at": next_retry_at,
                },
            )
        )

    def conclude_intent(
        self, task_id: str, intent_id: str, worker: str, description: str
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/internal/tasks/{task_id}/intents/{intent_id}/conclude",
            {"worker": worker, "description": description},
        )

    def needs_replan(self, task_id: str, intent_id: str) -> Intent:
        return Intent.model_validate(
            self._request(
                "POST",
                f"/internal/tasks/{task_id}/intents/{intent_id}/needs-replan",
                {"reason": "Explore retry budget exhausted"},
            )
        )

    def claim_reason(self, task_id: str, worker: str) -> Task:
        return Task.model_validate(
            self._request("POST", f"/internal/tasks/{task_id}/reason/claim", {"worker": worker})
        )

    def heartbeat_reason(self, task_id: str, worker: str) -> Task:
        return Task.model_validate(
            self._request("POST", f"/internal/tasks/{task_id}/reason/heartbeat", {"worker": worker})
        )

    def release_reason(self, task_id: str, worker: str) -> Task:
        return Task.model_validate(
            self._request("POST", f"/internal/tasks/{task_id}/reason/release", {"worker": worker})
        )

    def open_workspace(
        self, task_id: str, *, worker: str, context_revision: int
    ) -> ReasonWorkspace:
        return ReasonWorkspace.model_validate(
            self._request(
                "POST",
                f"/tasks/{task_id}/reason/workspaces",
                {"worker": worker, "context_revision": context_revision},
            )
        )

    def workspace(self, workspace_id: str) -> ReasonWorkspace:
        return ReasonWorkspace.model_validate(
            self._request("GET", f"/reason-workspaces/{workspace_id}")
        )

    def abandon_workspace(
        self, workspace_id: str, *, worker: str, failure_reason: str
    ) -> ReasonWorkspace:
        return ReasonWorkspace.model_validate(
            self._request(
                "POST",
                f"/reason-workspaces/{workspace_id}/abandon",
                {"worker": worker, "failure_reason": failure_reason},
            )
        )

    def set_worker_cooldown(
        self, worker: str, *, failure_kind: str, cooldown_until: str | None
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/internal/dispatcher/workers/{worker}/cooldown",
            {"failure_kind": failure_kind, "cooldown_until": cooldown_until},
        )

    def audit(self, body: dict[str, Any]) -> None:
        self._request("POST", "/internal/audit-events", body)

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        internal: bool = True,
    ) -> Any:
        headers = {"Content-Type": "application/json"}
        if internal and self.service_token:
            headers["Authorization"] = f"Bearer {self.service_token}"
        try:
            response = self._session().request(
                method,
                f"{self.base_url}/api/v1{path}",
                json=body,
                headers=headers,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise V2ProtocolError(str(exc)) from exc
        if not response.ok:
            try:
                detail = response.json().get("detail", response.text)
            except Exception:
                detail = response.text
            raise V2ProtocolError(f"HTTP {response.status_code}: {detail}", status_code=response.status_code)
        if not response.content:
            return None
        return response.json()

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is not None:
            return session
        session = requests.Session()
        adapter = HTTPAdapter(pool_connections=32, pool_maxsize=32, pool_block=False)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        self._local.session = session
        with self._lock:
            self._sessions.append(session)
        return session
