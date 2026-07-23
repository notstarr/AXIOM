from __future__ import annotations

from sqlalchemy import Engine, select

from axiom.infrastructure.database.metadata import intents
from axiom.modules.audit.schemas import RecordAuditEvent
from axiom.modules.audit.service import AuditService
from axiom.modules.common.time import utcnow
from axiom.modules.tasks.repository import TaskRepository
from axiom.modules.vulnerabilities.repository import VulnerabilityRepository
from axiom.modules.vulnerabilities.schemas import ReportVulnerability, UpdateVulnerability, Vulnerability


class VulnerabilityService:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.repository = VulnerabilityRepository()
        self.tasks = TaskRepository()

    def report(self, task_id: str, body: ReportVulnerability) -> Vulnerability:
        self._validate_status_reason(body.status, body.status_reason)
        with self.engine.begin() as conn:
            task = self.tasks.get(conn, task_id)
            if task is None:
                raise ValueError(f"Task not found: {task_id}")
            if task.legacy_readonly:
                raise ValueError("Legacy task is read-only")
            if body.intent_id:
                exists = conn.execute(
                    select(intents.c.id).where(
                        intents.c.task_id == task_id,
                        intents.c.id == body.intent_id,
                    )
                ).scalar_one_or_none()
                if exists is None:
                    raise ValueError(f"Intent not found: {body.intent_id}")
            vulnerability = self.repository.create(conn, task_id, body, now=utcnow())
        AuditService(self.engine).record(
            RecordAuditEvent(
                project_id=task.project_id,
                task_id=task_id,
                intent_id=body.intent_id,
                worker=body.reported_by,
                event_type="vulnerability.reported",
                message=f"Vulnerability {vulnerability.id} reported",
                payload={"vulnerability_id": vulnerability.id, "severity": vulnerability.severity},
            )
        )
        return vulnerability

    def get(self, task_id: str, vuln_id: str) -> Vulnerability:
        with self.engine.connect() as conn:
            vuln = self.repository.get(conn, task_id, vuln_id)
        if vuln is None:
            raise ValueError(f"Vulnerability not found: {vuln_id}")
        return vuln

    def list(self, task_id: str) -> list[Vulnerability]:
        with self.engine.connect() as conn:
            return self.repository.list(conn, task_id)

    def list_all(
        self,
        *,
        project_id: str | None = None,
        task_id: str | None = None,
        search: str = "",
        status: str | None = None,
        severity: str | None = None,
        include_archived: bool = False,
    ) -> list[Vulnerability]:
        with self.engine.connect() as conn:
            return self.repository.list_all(
                conn,
                project_id=project_id,
                task_id=task_id,
                search=search,
                status=status,
                severity=severity,
                include_archived=include_archived,
            )

    def update(self, task_id: str, vuln_id: str, body: UpdateVulnerability) -> Vulnerability:
        with self.engine.begin() as conn:
            task = self.tasks.get(conn, task_id)
            if task is None:
                raise ValueError(f"Task not found: {task_id}")
            if task.legacy_readonly:
                raise ValueError("Legacy task is read-only")
            current = self.repository.get(conn, task_id, vuln_id)
            if current is None:
                raise ValueError(f"Vulnerability not found: {vuln_id}")
            next_status = body.status or current.status
            next_reason = body.status_reason if body.status_reason is not None else current.status_reason
            self._validate_status_reason(next_status, next_reason)
            updated = self.repository.update(conn, task_id, vuln_id, body, now=utcnow())
            assert updated is not None
            return updated

    def archive(self, task_id: str, vuln_id: str) -> None:
        with self.engine.begin() as conn:
            task = self.tasks.get(conn, task_id)
            if task is None:
                raise ValueError(f"Task not found: {task_id}")
            if task.legacy_readonly:
                raise ValueError("Legacy task is read-only")
            if not self.repository.archive(conn, task_id, vuln_id, now=utcnow()):
                raise ValueError(f"Vulnerability not found: {vuln_id}")

    @staticmethod
    def _validate_status_reason(status: str, reason: str) -> None:
        if status == "忽略" and len(reason.strip()) < 4:
            raise ValueError("Ignored vulnerability requires a status reason of at least 4 characters")
