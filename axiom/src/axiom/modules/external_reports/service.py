from __future__ import annotations

from collections.abc import Callable

import httpx
from sqlalchemy import Engine

from axiom.modules.external_reports.repository import ExternalReportRepository
from axiom.modules.external_reports.schemas import (
    ExternalReportAttempt,
    ExternalReportConfiguration,
    ExternalReportPreview,
    ExternalReportResult,
)
from axiom.modules.settings.service import RuntimeSettingsService
from axiom.modules.tasks.repository import TaskRepository
from axiom.modules.vulnerabilities.repository import VulnerabilityRepository
from axiom.modules.vulnerabilities.schemas import Vulnerability, VulnerabilityReport
from axiom.shared.clock import utcnow
from axiom.shared.database import immediate_transaction
from axiom.shared.errors import ConflictError, NotFoundError


SendReport = Callable[[str, dict, dict[str, str], object], tuple[int, str]]


def send_external_report(
    endpoint: str,
    payload: dict,
    headers: dict[str, str],
    config,
) -> tuple[int, str]:
    """Send one report; the function boundary is injectable in deterministic tests."""

    client_kwargs: dict[str, object] = {"timeout": config.external_report_timeout_seconds}
    if config.network_proxy_mode == "http":
        client_kwargs["proxy"] = config.network_proxy_url
    with httpx.Client(headers=headers, **client_kwargs) as client:
        response = client.post(endpoint, json=payload)
    return response.status_code, response.text[-20_000:]


class ExternalReportService:
    def __init__(
        self,
        engine: Engine,
        settings: RuntimeSettingsService,
        *,
        sender: SendReport = send_external_report,
    ):
        self.engine = engine
        self.settings = settings
        self.sender = sender
        self.repository = ExternalReportRepository()
        self.tasks = TaskRepository()
        self.vulnerabilities = VulnerabilityRepository()

    def configuration(self, task_id: str) -> ExternalReportConfiguration:
        with self.engine.connect() as conn:
            task = self.tasks.get(conn, task_id)
        if task is None:
            raise NotFoundError(f"Task not found: {task_id}")
        config = self.settings.get().config
        return ExternalReportConfiguration(
            enabled=task.external_report_enabled,
            endpoint_configured=bool(config.external_report_base_url.strip()),
            token_configured=bool(config.external_report_token.strip()),
            max_attempts=config.external_report_max_attempts,
        )

    def preview(self, task_id: str, vulnerability_id: str) -> ExternalReportPreview:
        vulnerability = self._vulnerability(task_id, vulnerability_id)
        endpoint, payload, _headers = self._request(vulnerability)
        return ExternalReportPreview(
            endpoint=endpoint,
            payload=payload,
            headers={"Content-Type": "application/json"},
        )

    def submit(self, task_id: str, vulnerability_id: str) -> ExternalReportResult:
        return self._submit(task_id, vulnerability_id)

    def retry(self, task_id: str, vulnerability_id: str) -> ExternalReportResult:
        return self._submit(task_id, vulnerability_id, retry_only=True)

    def attempts(self, task_id: str, vulnerability_id: str) -> list[ExternalReportAttempt]:
        self._vulnerability(task_id, vulnerability_id)
        with self.engine.connect() as conn:
            return self.repository.list(conn, task_id=task_id, vulnerability_id=vulnerability_id)

    def _submit(
        self,
        task_id: str,
        vulnerability_id: str,
        *,
        retry_only: bool = False,
    ) -> ExternalReportResult:
        vulnerability = self._vulnerability(task_id, vulnerability_id)
        config = self.settings.get().config
        with immediate_transaction(self.engine) as conn:
            task = self.tasks.get(conn, task_id)
            assert task is not None
            if not task.external_report_enabled:
                raise ConflictError("External reporting is disabled for this Task")
            endpoint, payload, headers = self._request(vulnerability, config=config)
            attempts = self.repository.list(conn, task_id=task_id, vulnerability_id=vulnerability_id)
            if retry_only and (not attempts or attempts[0].status != "failed"):
                raise ConflictError("Only a failed external report can be retried")
            if attempts and attempts[0].status == "running":
                raise ConflictError("An external report is already running")
            next_no = self.repository.next_attempt_no(
                conn,
                task_id=task_id,
                vulnerability_id=vulnerability_id,
            )
            if next_no > config.external_report_max_attempts:
                raise ConflictError("External report retry limit reached")
            attempt = self.repository.create_running(
                conn,
                task_id=task_id,
                vulnerability_id=vulnerability_id,
                attempt_no=next_no,
                endpoint=endpoint,
                payload=payload,
                now=utcnow(),
            )

        try:
            response_status, response_body = self.sender(endpoint, payload.model_dump(mode="json"), headers, config)
            status = "succeeded" if 200 <= response_status < 300 else "failed"
            error = "" if status == "succeeded" else f"External endpoint returned HTTP {response_status}"
        except Exception as exc:  # the durable attempt must capture transport failures
            response_status, response_body, status = None, "", "failed"
            error = str(exc).strip() or exc.__class__.__name__

        with self.engine.begin() as conn:
            finished = self.repository.finish(
                conn,
                attempt_id=attempt.id,
                status=status,
                response_status=response_status,
                response_body=response_body,
                error=error[-4_000:],
                now=utcnow(),
            )
        if finished is None:
            raise ConflictError("External report attempt was modified concurrently")
        return ExternalReportResult(attempt=finished, accepted=status == "succeeded")

    def _vulnerability(self, task_id: str, vulnerability_id: str) -> Vulnerability:
        with self.engine.connect() as conn:
            task = self.tasks.get(conn, task_id)
            vulnerability = self.vulnerabilities.get(conn, task_id, vulnerability_id)
        if task is None:
            raise NotFoundError(f"Task not found: {task_id}")
        if vulnerability is None:
            raise NotFoundError(f"Vulnerability not found: {vulnerability_id}")
        return vulnerability

    def _request(self, vulnerability: Vulnerability, *, config=None):
        config = config or self.settings.get().config
        endpoint = config.external_report_base_url.strip().rstrip("/")
        if not endpoint:
            raise ConflictError("External report endpoint is not configured")
        report_fields = vulnerability.model_dump(
            mode="json",
            exclude={"id", "task_id", "created_at", "updated_at", "archived"},
        )
        payload = VulnerabilityReport.model_validate(report_fields)
        headers = {"Content-Type": "application/json"}
        token = config.external_report_token.strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return endpoint, payload, headers
