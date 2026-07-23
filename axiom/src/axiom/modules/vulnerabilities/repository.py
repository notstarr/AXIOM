from __future__ import annotations

import json

from sqlalchemy import Connection, or_, select, update

from axiom.modules.projects.tables import projects
from axiom.modules.tasks.tables import tasks
from axiom.modules.vulnerabilities.schemas import Vulnerability, VulnerabilityReport, UpdateVulnerability
from axiom.modules.vulnerabilities.tables import vulnerabilities
from axiom.shared.ids import allocate_id


class VulnerabilityRepository:
    def create(self, conn: Connection, *, task_id: str, body: VulnerabilityReport, now: str) -> Vulnerability:
        values = {
            "id": allocate_id(conn, scope=task_id, kind="vulnerability", prefix="v"),
            "task_id": task_id,
            "intent_id": body.intent_id,
            "title": body.title,
            "type": body.type,
            "severity": body.severity,
            "description": body.description,
            "prerequisites": body.prerequisites,
            "impact": body.impact,
            "reproduction_steps_json": json.dumps(body.reproduction_steps, ensure_ascii=False),
            "remediation": body.remediation,
            "curl_command": body.curl_command,
            "evidence_json": json.dumps(body.evidence, ensure_ascii=False),
            "affected_target": body.affected_target,
            "status": body.status,
            "status_reason": body.status_reason,
            "source": body.source,
            "reported_by": body.reported_by,
            "created_at": now,
            "updated_at": now,
            "archived": False,
        }
        conn.execute(vulnerabilities.insert().values(**values))
        return self._model(values)

    def get(self, conn: Connection, task_id: str, vulnerability_id: str) -> Vulnerability | None:
        row = conn.execute(
            select(vulnerabilities).where(
                vulnerabilities.c.task_id == task_id,
                vulnerabilities.c.id == vulnerability_id,
            )
        ).mappings().one_or_none()
        return self._model(row) if row is not None else None

    def list(self, conn: Connection, task_id: str) -> list[Vulnerability]:
        rows = conn.execute(
            select(vulnerabilities)
            .where(vulnerabilities.c.task_id == task_id, vulnerabilities.c.archived.is_(False))
            .order_by(vulnerabilities.c.updated_at.desc(), vulnerabilities.c.id.desc())
        ).mappings()
        return [self._model(row) for row in rows]

    def list_all(
        self,
        conn: Connection,
        *,
        project_id: str | None = None,
        task_id: str | None = None,
        search: str = "",
        status: str | None = None,
        severity: str | None = None,
        include_archived: bool = False,
    ) -> list[Vulnerability]:
        query = select(vulnerabilities).join(tasks, tasks.c.id == vulnerabilities.c.task_id)
        if project_id:
            query = query.where(tasks.c.project_id == project_id)
        if task_id:
            query = query.where(vulnerabilities.c.task_id == task_id)
        if status:
            query = query.where(vulnerabilities.c.status == status)
        if severity:
            query = query.where(vulnerabilities.c.severity == severity)
        if not include_archived:
            query = query.where(vulnerabilities.c.archived.is_(False))
        if search.strip():
            pattern = f"%{search.strip()}%"
            query = query.join(projects, projects.c.id == tasks.c.project_id).where(
                or_(
                    vulnerabilities.c.id.ilike(pattern),
                    vulnerabilities.c.title.ilike(pattern),
                    vulnerabilities.c.type.ilike(pattern),
                    vulnerabilities.c.description.ilike(pattern),
                    vulnerabilities.c.affected_target.ilike(pattern),
                    tasks.c.title.ilike(pattern),
                    projects.c.title.ilike(pattern),
                )
            )
        rows = conn.execute(
            query.order_by(vulnerabilities.c.updated_at.desc(), vulnerabilities.c.id.desc())
        ).mappings()
        return [self._model(row) for row in rows]

    def update(
        self,
        conn: Connection,
        task_id: str,
        vulnerability_id: str,
        body: UpdateVulnerability,
        *,
        now: str,
    ) -> Vulnerability | None:
        values = body.model_dump(exclude_none=True)
        if "reproduction_steps" in values:
            values["reproduction_steps_json"] = json.dumps(
                values.pop("reproduction_steps"), ensure_ascii=False
            )
        if "evidence" in values:
            values["evidence_json"] = json.dumps(values.pop("evidence"), ensure_ascii=False)
        if not values:
            return self.get(conn, task_id, vulnerability_id)
        values["updated_at"] = now
        result = conn.execute(
            update(vulnerabilities)
            .where(
                vulnerabilities.c.task_id == task_id,
                vulnerabilities.c.id == vulnerability_id,
            )
            .values(**values)
        )
        return self.get(conn, task_id, vulnerability_id) if result.rowcount else None

    def archive(self, conn: Connection, task_id: str, vulnerability_id: str, *, now: str) -> bool:
        result = conn.execute(
            update(vulnerabilities)
            .where(
                vulnerabilities.c.task_id == task_id,
                vulnerabilities.c.id == vulnerability_id,
            )
            .values(archived=True, updated_at=now)
        )
        return bool(result.rowcount)

    @staticmethod
    def _model(row) -> Vulnerability:
        return Vulnerability(
            id=row["id"],
            task_id=row["task_id"],
            intent_id=row["intent_id"],
            title=row["title"],
            type=row["type"],
            severity=row["severity"],
            description=row["description"],
            prerequisites=row["prerequisites"],
            impact=row["impact"],
            reproduction_steps=json.loads(row["reproduction_steps_json"] or "[]"),
            remediation=row["remediation"],
            curl_command=row["curl_command"],
            evidence=json.loads(row["evidence_json"] or "[]"),
            affected_target=row["affected_target"],
            status=row["status"],
            status_reason=row["status_reason"],
            source=row["source"],
            reported_by=row["reported_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            archived=bool(row["archived"]),
        )
