from __future__ import annotations

import json
import uuid
from typing import Any

import requests
from pydantic import BaseModel
from sqlalchemy import Engine, select, update

from axiom.infrastructure.database.metadata import external_report_attempts
from axiom.modules.audit.schemas import RecordAuditEvent
from axiom.modules.audit.service import AuditService
from axiom.modules.common.time import utcnow
from axiom.modules.settings.service import RuntimeSettingsService
from axiom.modules.tasks.repository import TaskRepository
from axiom.modules.vulnerabilities.external import MozzieAdapter
from axiom.modules.vulnerabilities.service import VulnerabilityService


class ExternalReportAttempt(BaseModel):
    id: str
    task_id: str
    vulnerability_id: str
    platform: str
    resolved_project: str
    payload: dict[str, Any]
    status: str
    attempt_count: int
    last_error: str
    response: dict[str, Any]
    created_at: str
    updated_at: str


class MozzieClient:
    def __init__(self, *, base_url: str, token: str, timeout_seconds: int, session=None):
        self.base_url = base_url.rstrip("/")
        self.token = token.strip()
        self.timeout_seconds = timeout_seconds
        self.session = session or requests.Session()

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    def resolve_project(self, backend_domain: str) -> str:
        if not self.token:
            return "others"
        try:
            response = self.session.get(
                f"{self.base_url}/api/ingress-info",
                params={"page": 1, "limit": 20, "search": backend_domain},
                headers=self.headers,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            project = _first_project(response.json())
            return project or "others"
        except Exception:
            return "others"

    def report(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.token:
            raise RuntimeError("Mozzie token is not configured")
        response = self.session.post(
            f"{self.base_url}/api/public/vulnerabilities",
            json=payload,
            headers=self.headers,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        try:
            body = response.json()
        except Exception:
            body = {"raw": response.text}
        if isinstance(body, dict) and body.get("success") is False:
            raise RuntimeError(f"Mozzie rejected report: {body}")
        return body if isinstance(body, dict) else {"data": body}


class ExternalReportService:
    PLATFORM = "mozzie"

    def __init__(self, engine: Engine, *, client: MozzieClient | None = None):
        self.engine = engine
        self._client = client
        self.tasks = TaskRepository()
        self.vulnerabilities = VulnerabilityService(engine)

    def preview(
        self,
        task_id: str,
        vulnerability_id: str,
        *,
        resolved_project: str | None = None,
    ) -> dict[str, Any]:
        task, vulnerability = self._load(task_id, vulnerability_id)
        project = (resolved_project or "").strip()
        if not project:
            project = self._get_client().resolve_project(
                vulnerability.affected_target or task.origin.host
            )
        return MozzieAdapter().build_payload(vulnerability, resolved_project=project)

    def report(
        self,
        task_id: str,
        vulnerability_id: str,
        *,
        resolved_project: str | None = None,
    ) -> ExternalReportAttempt:
        task, vulnerability = self._load(task_id, vulnerability_id)
        client = self._get_client()
        project = (resolved_project or "").strip() or client.resolve_project(
            vulnerability.affected_target or task.origin.host
        )
        payload = MozzieAdapter().build_payload(vulnerability, resolved_project=project)
        now = utcnow()
        attempt_id = f"era_{uuid.uuid4().hex}"
        with self.engine.begin() as conn:
            conn.execute(
                external_report_attempts.insert().values(
                    id=attempt_id,
                    task_id=task_id,
                    vulnerability_id=vulnerability_id,
                    platform=self.PLATFORM,
                    resolved_project=project,
                    payload_json=json.dumps(payload, ensure_ascii=False),
                    status="pending",
                    attempt_count=0,
                    last_error="",
                    response_json="{}",
                    created_at=now,
                    updated_at=now,
                )
            )
        return self.retry(attempt_id)

    def retry(self, attempt_id: str) -> ExternalReportAttempt:
        with self.engine.connect() as conn:
            row = conn.execute(
                select(external_report_attempts).where(external_report_attempts.c.id == attempt_id)
            ).mappings().one_or_none()
        if row is None:
            raise ValueError(f"External report attempt not found: {attempt_id}")
        max_attempts = RuntimeSettingsService(self.engine).get().config.external_report_max_attempts
        if row["status"] == "success":
            return self._model(row)
        if int(row["attempt_count"]) >= max_attempts:
            raise ValueError(f"External report attempt exhausted: {attempt_id}")

        count = int(row["attempt_count"]) + 1
        now = utcnow()
        try:
            response = self._get_client().report(json.loads(row["payload_json"]))
            status, error = "success", ""
        except Exception as exc:
            response = {}
            status, error = "failed", str(exc)
        with self.engine.begin() as conn:
            conn.execute(
                update(external_report_attempts)
                .where(external_report_attempts.c.id == attempt_id)
                .values(
                    status=status,
                    attempt_count=count,
                    last_error=error,
                    response_json=json.dumps(response, ensure_ascii=False),
                    updated_at=now,
                )
            )
            updated = conn.execute(
                select(external_report_attempts).where(external_report_attempts.c.id == attempt_id)
            ).mappings().one()
        attempt = self._model(updated)
        with self.engine.connect() as conn:
            task = self.tasks.get(conn, attempt.task_id)
        AuditService(self.engine).record(
            RecordAuditEvent(
                project_id=task.project_id if task else None,
                task_id=attempt.task_id,
                event_type=f"external_report.{attempt.status}",
                level="info" if attempt.status == "success" else "warning",
                message=f"External report attempt {attempt.id} {attempt.status}",
                payload={
                    "attempt_id": attempt.id,
                    "vulnerability_id": attempt.vulnerability_id,
                    "attempt_count": attempt.attempt_count,
                    "platform": attempt.platform,
                    "error": attempt.last_error,
                },
            )
        )
        return attempt

    def list(self, task_id: str, vulnerability_id: str | None = None) -> list[ExternalReportAttempt]:
        query = select(external_report_attempts).where(external_report_attempts.c.task_id == task_id)
        if vulnerability_id:
            query = query.where(
                external_report_attempts.c.vulnerability_id == vulnerability_id
            )
        with self.engine.connect() as conn:
            rows = conn.execute(
                query.order_by(external_report_attempts.c.created_at.desc())
            ).mappings()
            return [self._model(row) for row in rows]

    def _load(self, task_id: str, vulnerability_id: str):
        with self.engine.connect() as conn:
            task = self.tasks.get(conn, task_id)
        if task is None:
            raise ValueError(f"Task not found: {task_id}")
        vulnerability = self.vulnerabilities.get(task_id, vulnerability_id)
        return task, vulnerability

    def _get_client(self) -> MozzieClient:
        if self._client is not None:
            return self._client
        config = RuntimeSettingsService(self.engine).get().config
        return MozzieClient(
            base_url=config.mozzie_base_url,
            token=config.mozzie_token,
            timeout_seconds=config.mozzie_timeout_seconds,
        )

    @staticmethod
    def _model(row) -> ExternalReportAttempt:
        return ExternalReportAttempt(
            id=row["id"],
            task_id=row["task_id"],
            vulnerability_id=row["vulnerability_id"],
            platform=row["platform"],
            resolved_project=row["resolved_project"],
            payload=json.loads(row["payload_json"]),
            status=row["status"],
            attempt_count=row["attempt_count"],
            last_error=row["last_error"],
            response=json.loads(row["response_json"] or "{}"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


def _first_project(value: Any) -> str | None:
    if isinstance(value, dict):
        project = value.get("project")
        if isinstance(project, str) and project.strip():
            return project.strip()
        for key in ("data", "items", "list", "rows", "result"):
            if key in value:
                found = _first_project(value[key])
                if found:
                    return found
    elif isinstance(value, list):
        for item in value:
            found = _first_project(item)
            if found:
                return found
    return None
