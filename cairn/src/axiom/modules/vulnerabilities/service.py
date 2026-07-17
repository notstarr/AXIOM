from __future__ import annotations

from sqlalchemy import Engine, select

from axiom.infrastructure.database.metadata import intents
from axiom.modules.common.time import utcnow
from axiom.modules.tasks.repository import TaskRepository
from axiom.modules.vulnerabilities.repository import VulnerabilityRepository
from axiom.modules.vulnerabilities.schemas import ReportVulnerability, Vulnerability


class VulnerabilityService:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.repository = VulnerabilityRepository()
        self.tasks = TaskRepository()

    def report(self, task_id: str, body: ReportVulnerability) -> Vulnerability:
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
            return self.repository.create(conn, task_id, body, now=utcnow())

    def get(self, task_id: str, vuln_id: str) -> Vulnerability:
        with self.engine.connect() as conn:
            vuln = self.repository.get(conn, task_id, vuln_id)
        if vuln is None:
            raise ValueError(f"Vulnerability not found: {vuln_id}")
        return vuln

    def list(self, task_id: str) -> list[Vulnerability]:
        with self.engine.connect() as conn:
            return self.repository.list(conn, task_id)
