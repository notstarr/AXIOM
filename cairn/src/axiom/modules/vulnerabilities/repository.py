from __future__ import annotations

import json

from sqlalchemy import Connection, select

from axiom.infrastructure.database.metadata import vulnerabilities
from axiom.modules.common.ids import allocate_readable_id
from axiom.modules.vulnerabilities.schemas import ReportVulnerability, Vulnerability


class VulnerabilityRepository:
    def create(
        self,
        conn: Connection,
        task_id: str,
        body: ReportVulnerability,
        *,
        now: str,
    ) -> Vulnerability:
        vuln_id = allocate_readable_id(conn, scope=task_id, kind="vulnerability", prefix="v")
        values = {
            "id": vuln_id,
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
            "source": body.source,
            "reported_by": body.reported_by,
            "created_at": now,
            "updated_at": now,
            "archived": False,
        }
        conn.execute(vulnerabilities.insert().values(**values))
        return self._to_model(values)

    def get(self, conn: Connection, task_id: str, vuln_id: str) -> Vulnerability | None:
        row = conn.execute(
            select(vulnerabilities).where(
                vulnerabilities.c.task_id == task_id,
                vulnerabilities.c.id == vuln_id,
            )
        ).mappings().one_or_none()
        return self._to_model(row) if row is not None else None

    def list(self, conn: Connection, task_id: str) -> list[Vulnerability]:
        rows = conn.execute(
            select(vulnerabilities)
            .where(vulnerabilities.c.task_id == task_id, vulnerabilities.c.archived.is_(False))
            .order_by(vulnerabilities.c.created_at, vulnerabilities.c.id)
        ).mappings()
        return [self._to_model(row) for row in rows]

    @staticmethod
    def _to_model(row) -> Vulnerability:
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
            source=row["source"],
            reported_by=row["reported_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            archived=bool(row["archived"]),
        )
