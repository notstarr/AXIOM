from __future__ import annotations

from sqlalchemy import Engine, select

from axiom.modules.graph.tables import intents
from axiom.modules.tasks.repository import TaskRepository
from axiom.modules.vulnerabilities.repository import VulnerabilityRepository
from axiom.modules.vulnerabilities.schemas import Vulnerability, VulnerabilityReport, UpdateVulnerability
from axiom.shared.clock import utcnow
from axiom.shared.errors import ConflictError, NotFoundError


class VulnerabilityService:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.repository = VulnerabilityRepository()
        self.tasks = TaskRepository()

    def report(self, task_id: str, body: VulnerabilityReport) -> Vulnerability:
        self._validate_status_reason(body.status, body.status_reason)
        with self.engine.begin() as conn:
            task = self.tasks.get(conn, task_id)
            if task is None:
                raise NotFoundError(f"Task not found: {task_id}")
            if task.legacy_readonly:
                raise ConflictError("Legacy Task is read-only")
            if body.intent_id and conn.execute(
                select(intents.c.id).where(
                    intents.c.task_id == task_id,
                    intents.c.id == body.intent_id,
                )
            ).scalar_one_or_none() is None:
                raise NotFoundError(f"Intent not found: {body.intent_id}")
            return self.repository.create(conn, task_id=task_id, body=body, now=utcnow())

    def get(self, task_id: str, vulnerability_id: str) -> Vulnerability:
        with self.engine.connect() as conn:
            result = self.repository.get(conn, task_id, vulnerability_id)
        if result is None:
            raise NotFoundError(f"Vulnerability not found: {vulnerability_id}")
        return result

    def list(self, task_id: str) -> list[Vulnerability]:
        with self.engine.connect() as conn:
            if self.tasks.get(conn, task_id) is None:
                raise NotFoundError(f"Task not found: {task_id}")
            return self.repository.list(conn, task_id)

    def list_all(self, **filters) -> list[Vulnerability]:
        with self.engine.connect() as conn:
            return self.repository.list_all(conn, **filters)

    def update(self, task_id: str, vulnerability_id: str, body: UpdateVulnerability) -> Vulnerability:
        with self.engine.begin() as conn:
            task = self.tasks.get(conn, task_id)
            if task is None:
                raise NotFoundError(f"Task not found: {task_id}")
            current = self.repository.get(conn, task_id, vulnerability_id)
            if current is None:
                raise NotFoundError(f"Vulnerability not found: {vulnerability_id}")
            self._validate_status_reason(
                body.status or current.status,
                body.status_reason if body.status_reason is not None else current.status_reason,
            )
            updated = self.repository.update(
                conn,
                task_id,
                vulnerability_id,
                body,
                now=utcnow(),
            )
            assert updated is not None
            return updated

    def archive(self, task_id: str, vulnerability_id: str) -> None:
        with self.engine.begin() as conn:
            if self.tasks.get(conn, task_id) is None:
                raise NotFoundError(f"Task not found: {task_id}")
            if not self.repository.archive(conn, task_id, vulnerability_id, now=utcnow()):
                raise NotFoundError(f"Vulnerability not found: {vulnerability_id}")

    @staticmethod
    def _validate_status_reason(status: str, reason: str) -> None:
        if status == "忽略" and len(reason.strip()) < 4:
            raise ConflictError("Ignored vulnerability requires a status reason")
