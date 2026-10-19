from __future__ import annotations

import json

from sqlalchemy import Connection, case, delete, func, or_, select, update

from axiom.modules.artifacts.schemas import Artifact
from axiom.modules.artifacts.tables import artifacts
from axiom.modules.projects.tables import projects
from axiom.modules.tasks.tables import tasks
from axiom.modules.vulnerabilities.schemas import (
    LegacyVulnerabilityDetails,
    UpdateVulnerability,
    Vulnerability,
    VulnerabilityReport,
)
from axiom.modules.vulnerabilities.tables import vulnerability_artifacts, vulnerabilities
from axiom.shared.ids import allocate_id


class VulnerabilityRepository:
    @staticmethod
    def _order_by(sort: str) -> list:
        """排序预设：updated（默认）/severity/unreviewed/newest。"""
        severity_rank = case(
            (vulnerabilities.c.severity == "critical", 0),
            (vulnerabilities.c.severity == "high", 1),
            (vulnerabilities.c.severity == "medium", 2),
            (vulnerabilities.c.severity == "low", 3),
            else_=4,
        )
        if sort == "severity":
            return [
                severity_rank.asc(),
                vulnerabilities.c.updated_at.desc(),
                vulnerabilities.c.id.desc(),
            ]
        if sort == "unreviewed":
            return [
                vulnerabilities.c.human_reviewed.asc(),
                severity_rank.asc(),
                vulnerabilities.c.updated_at.desc(),
                vulnerabilities.c.id.desc(),
            ]
        if sort == "newest":
            return [
                vulnerabilities.c.created_at.desc(),
                vulnerabilities.c.id.desc(),
            ]
        return [
            vulnerabilities.c.updated_at.desc(),
            vulnerabilities.c.id.desc(),
        ]

    def create(
        self,
        conn: Connection,
        *,
        task_id: str,
        body: VulnerabilityReport,
        now: str,
    ) -> Vulnerability:
        vulnerability_id = allocate_id(conn, scope=task_id, kind="vulnerability", prefix="v")
        values = {
            "id": vulnerability_id,
            "task_id": task_id,
            "intent_id": body.intent_id,
            "title": body.title,
            "type": body.type,
            "severity": body.severity,
            "description": body.description,
            "prerequisites": body.prerequisites,
            "impact": body.impact,
            # V1 columns remain solely for read-only historical compatibility.
            "reproduction_steps_json": "[]",
            "remediation": body.remediation,
            "curl_command": "",
            "evidence_json": "[]",
            "affected_target": body.affected_target,
            "status": body.status,
            "status_reason": body.status_reason,
            "source": body.source,
            "reported_by": body.reported_by,
            "created_at": now,
            "updated_at": now,
            "archived": False,
            "format_version": 2,
        }
        conn.execute(vulnerabilities.insert().values(**values))
        self._replace_artifacts(
            conn,
            task_id=task_id,
            vulnerability_id=vulnerability_id,
            finding_artifact_id=body.finding_artifact_id,
            evidence_artifact_ids=body.evidence_artifact_ids,
        )
        result = self.get(conn, task_id, vulnerability_id)
        assert result is not None
        return result

    def get(self, conn: Connection, task_id: str, vulnerability_id: str) -> Vulnerability | None:
        row = conn.execute(
            select(vulnerabilities).where(
                vulnerabilities.c.task_id == task_id,
                vulnerabilities.c.id == vulnerability_id,
            )
        ).mappings().one_or_none()
        return self._models(conn, [row])[0] if row is not None else None

    def list(self, conn: Connection, task_id: str) -> list[Vulnerability]:
        rows = list(
            conn.execute(
                select(vulnerabilities)
                .where(vulnerabilities.c.task_id == task_id, vulnerabilities.c.archived.is_(False))
                .order_by(vulnerabilities.c.updated_at.desc(), vulnerabilities.c.id.desc())
            ).mappings()
        )
        return self._models(conn, rows)

    def list_all(
        self,
        conn: Connection,
        *,
        project_id: str | None = None,
        task_id: str | None = None,
        search: str = "",
        status: str | None = None,
        severity: str | None = None,
        red_team_exploitable: bool | None = None,
        include_archived: bool = False,
        sort: str = "updated",
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
        if red_team_exploitable is not None:
            query = query.where(
                vulnerabilities.c.red_team_exploitable.is_(red_team_exploitable)
            )
        if not include_archived:
            query = query.where(
                tasks.c.archived.is_(False),
                vulnerabilities.c.archived.is_(False),
            )
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
        rows = list(
            conn.execute(
                query.order_by(*self._order_by(sort))
            ).mappings()
        )
        return self._models(conn, rows)

    def list_page(
        self,
        conn: Connection,
        *,
        project_id: str | None = None,
        task_id: str | None = None,
        search: str = "",
        status: str | None = None,
        severity: str | None = None,
        red_team_exploitable: bool | None = None,
        include_archived: bool = False,
        limit: int,
        offset: int,
        sort: str = "updated",
    ) -> tuple[list[Vulnerability], int]:
        query = select(vulnerabilities).join(tasks, tasks.c.id == vulnerabilities.c.task_id)
        if project_id:
            query = query.where(tasks.c.project_id == project_id)
        if task_id:
            query = query.where(vulnerabilities.c.task_id == task_id)
        if status:
            query = query.where(vulnerabilities.c.status == status)
        if severity:
            query = query.where(vulnerabilities.c.severity == severity)
        if red_team_exploitable is not None:
            query = query.where(
                vulnerabilities.c.red_team_exploitable.is_(red_team_exploitable)
            )
        if not include_archived:
            query = query.where(
                tasks.c.archived.is_(False),
                vulnerabilities.c.archived.is_(False),
            )
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
        total = conn.execute(
            select(func.count()).select_from(query.order_by(None).subquery())
        ).scalar_one()
        rows = list(
            conn.execute(
                query.order_by(*self._order_by(sort))
                .limit(limit)
                .offset(offset)
            ).mappings()
        )
        return self._models(conn, rows), int(total)

    def update(
        self,
        conn: Connection,
        task_id: str,
        vulnerability_id: str,
        body: UpdateVulnerability,
        *,
        now: str,
    ) -> Vulnerability | None:
        values = body.model_dump(exclude_unset=True)
        finding_changed = "finding_artifact_id" in values
        evidence_changed = "evidence_artifact_ids" in values
        finding_artifact_id = values.pop("finding_artifact_id", None)
        evidence_artifact_ids = values.pop("evidence_artifact_ids", None)

        if values:
            values["updated_at"] = now
            result = conn.execute(
                update(vulnerabilities)
                .where(
                    vulnerabilities.c.task_id == task_id,
                    vulnerabilities.c.id == vulnerability_id,
                )
                .values(**values)
            )
            if not result.rowcount:
                return None
        elif self.get(conn, task_id, vulnerability_id) is None:
            return None

        if finding_changed or evidence_changed:
            current = self.get(conn, task_id, vulnerability_id)
            if current is None:
                return None
            self._replace_artifacts(
                conn,
                task_id=task_id,
                vulnerability_id=vulnerability_id,
                finding_artifact_id=(
                    finding_artifact_id if finding_changed else current.finding_artifact_id
                ),
                evidence_artifact_ids=(
                    list(evidence_artifact_ids or [])
                    if evidence_changed
                    else list(current.evidence_artifact_ids)
                ),
            )
            if not values:
                conn.execute(
                    update(vulnerabilities)
                    .where(
                        vulnerabilities.c.task_id == task_id,
                        vulnerabilities.c.id == vulnerability_id,
                    )
                    .values(updated_at=now)
                )
        return self.get(conn, task_id, vulnerability_id)

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

    def _models(self, conn: Connection, rows: list) -> list[Vulnerability]:
        if not rows:
            return []
        keys = {(str(row["task_id"]), str(row["id"])) for row in rows}
        task_ids = {task_id for task_id, _vulnerability_id in keys}
        vulnerability_ids = {vulnerability_id for _task_id, vulnerability_id in keys}
        relation_rows = list(
            conn.execute(
                select(vulnerability_artifacts).where(
                    vulnerability_artifacts.c.task_id.in_(task_ids),
                    vulnerability_artifacts.c.vulnerability_id.in_(vulnerability_ids),
                )
            ).mappings()
        )
        artifact_keys = {
            (str(row["task_id"]), str(row["artifact_id"])) for row in relation_rows
        }
        artifact_by_key: dict[tuple[str, str], Artifact] = {}
        if artifact_keys:
            artifact_task_ids = {task_id for task_id, _artifact_id in artifact_keys}
            artifact_ids = {artifact_id for _task_id, artifact_id in artifact_keys}
            artifact_rows = conn.execute(
                select(artifacts).where(
                    artifacts.c.task_id.in_(artifact_task_ids),
                    artifacts.c.id.in_(artifact_ids),
                )
            ).mappings()
            artifact_by_key = {
                (str(row["task_id"]), str(row["id"])): Artifact.model_validate(row)
                for row in artifact_rows
            }

        relations_by_vulnerability: dict[tuple[str, str], list] = {}
        for relation in relation_rows:
            relations_by_vulnerability.setdefault(
                (str(relation["task_id"]), str(relation["vulnerability_id"])), []
            ).append(relation)

        result: list[Vulnerability] = []
        for row in rows:
            key = (str(row["task_id"]), str(row["id"]))
            relations = sorted(
                relations_by_vulnerability.get(key, []),
                key=lambda item: (0 if item["role"] == "finding" else 1, item["display_order"]),
            )
            finding: Artifact | None = None
            evidence_items: list[Artifact] = []
            for relation in relations:
                artifact = artifact_by_key.get((key[0], str(relation["artifact_id"])))
                if artifact is None or artifact.archived:
                    continue
                if relation["role"] == "finding" and finding is None:
                    finding = artifact
                elif relation["role"] == "evidence":
                    evidence_items.append(artifact)

            format_version = int(row.get("format_version") or 1)
            legacy = None
            if format_version < 2:
                legacy = LegacyVulnerabilityDetails(
                    reproduction_steps=json.loads(row["reproduction_steps_json"] or "[]"),
                    curl_command=row["curl_command"] or "",
                    evidence=json.loads(row["evidence_json"] or "[]"),
                )
            result.append(
                Vulnerability(
                    id=row["id"],
                    task_id=row["task_id"],
                    intent_id=row["intent_id"],
                    title=row["title"],
                    type=row["type"],
                    severity=row["severity"],
                    description=row["description"],
                    prerequisites=row["prerequisites"],
                    impact=row["impact"],
                    remediation=row["remediation"],
                    affected_target=row["affected_target"],
                    status=row["status"],
                    status_reason=row["status_reason"],
                    source=row["source"],
                    reported_by=row["reported_by"],
                    finding_artifact_id=finding.id if finding else None,
                    evidence_artifact_ids=[item.id for item in evidence_items],
                    finding_artifact=finding,
                    evidence_artifacts=evidence_items,
                    format_version=format_version,
                    legacy=legacy,
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                    archived=bool(row["archived"]),
                    human_reviewed=bool(row.get("human_reviewed") or False),
                    human_reviewed_at=row.get("human_reviewed_at"),
                    human_reviewed_by=row.get("human_reviewed_by"),
                    red_team_exploitable=bool(row.get("red_team_exploitable") or False),
                    red_team_exploitable_at=row.get("red_team_exploitable_at"),
                    red_team_exploitable_by=row.get("red_team_exploitable_by"),
                )
            )
        return result

    def from_rows(self, conn: Connection, rows: list) -> dict[str, Vulnerability]:
        """Build vulnerability models and linked artifacts in batch."""

        return {vulnerability.id: vulnerability for vulnerability in self._models(conn, rows)}

    def set_human_reviewed(
        self,
        conn: Connection,
        task_id: str,
        vulnerability_id: str,
        *,
        value: bool,
        actor: str,
        now: str,
    ) -> Vulnerability | None:
        values: dict[str, object] = {
            "human_reviewed": value,
            "human_reviewed_at": now if value else None,
            "human_reviewed_by": actor if value else None,
            "updated_at": now,
        }
        result = conn.execute(
            update(vulnerabilities)
            .where(
                vulnerabilities.c.task_id == task_id,
                vulnerabilities.c.id == vulnerability_id,
            )
            .values(**values)
        )
        if not result.rowcount:
            return None
        found = self.get(conn, task_id, vulnerability_id)
        assert found is not None
        return found

    def set_red_team_exploitable(
        self,
        conn: Connection,
        task_id: str,
        vulnerability_id: str,
        *,
        value: bool,
        actor: str,
        now: str,
    ) -> Vulnerability | None:
        values: dict[str, object] = {
            "red_team_exploitable": value,
            "red_team_exploitable_at": now if value else None,
            "red_team_exploitable_by": actor if value else None,
            "updated_at": now,
        }
        result = conn.execute(
            update(vulnerabilities)
            .where(
                vulnerabilities.c.task_id == task_id,
                vulnerabilities.c.id == vulnerability_id,
            )
            .values(**values)
        )
        if not result.rowcount:
            return None
        found = self.get(conn, task_id, vulnerability_id)
        assert found is not None
        return found

    @staticmethod
    def _replace_artifacts(
        conn: Connection,
        *,
        task_id: str,
        vulnerability_id: str,
        finding_artifact_id: str | None,
        evidence_artifact_ids: list[str],
    ) -> None:
        conn.execute(
            delete(vulnerability_artifacts).where(
                vulnerability_artifacts.c.task_id == task_id,
                vulnerability_artifacts.c.vulnerability_id == vulnerability_id,
            )
        )
        rows: list[dict[str, object]] = []
        if finding_artifact_id:
            rows.append(
                {
                    "task_id": task_id,
                    "vulnerability_id": vulnerability_id,
                    "artifact_id": finding_artifact_id,
                    "role": "finding",
                    "display_order": 0,
                }
            )
        rows.extend(
            {
                "task_id": task_id,
                "vulnerability_id": vulnerability_id,
                "artifact_id": artifact_id,
                "role": "evidence",
                "display_order": order,
            }
            for order, artifact_id in enumerate(evidence_artifact_ids)
            if artifact_id != finding_artifact_id
        )
        if rows:
            conn.execute(vulnerability_artifacts.insert(), rows)
