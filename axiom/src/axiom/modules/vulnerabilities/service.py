from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Engine, select

from axiom.modules.artifacts.repository import ArtifactRepository
from axiom.modules.graph.tables import intents
from axiom.modules.tasks.repository import TaskRepository
from axiom.modules.vulnerabilities.repository import VulnerabilityRepository
from axiom.modules.vulnerabilities.schemas import Vulnerability, VulnerabilityReport, UpdateVulnerability
from axiom.shared.clock import utcnow
from axiom.shared.errors import ConflictError, NotFoundError

if TYPE_CHECKING:
    from axiom.modules.archives.service import ArchiveService


class VulnerabilityService:
    def __init__(self, engine: Engine, *, archives: ArchiveService | None = None):
        self.engine = engine
        self.repository = VulnerabilityRepository()
        self.artifacts = ArtifactRepository()
        self.tasks = TaskRepository()
        self.archives = archives

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
            self._validate_artifacts(
                conn,
                task_id=task_id,
                finding_artifact_id=body.finding_artifact_id,
                evidence_artifact_ids=body.evidence_artifact_ids,
            )
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
            if current.archived:
                raise ConflictError(
                    f"Archived Vulnerability cannot be updated: {vulnerability_id}"
                )
            self._validate_status_reason(
                body.status or current.status,
                body.status_reason if body.status_reason is not None else current.status_reason,
            )
            fields = body.model_fields_set
            finding_artifact_id = (
                body.finding_artifact_id
                if "finding_artifact_id" in fields
                else current.finding_artifact_id
            )
            evidence_artifact_ids = (
                body.evidence_artifact_ids
                if "evidence_artifact_ids" in fields
                else current.evidence_artifact_ids
            )
            self._validate_artifacts(
                conn,
                task_id=task_id,
                finding_artifact_id=finding_artifact_id,
                evidence_artifact_ids=list(evidence_artifact_ids or []),
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
        if self.archives is None:
            raise ConflictError("Archive service is unavailable")
        self.archives.create_compat(
            task_id,
            root_type="vulnerability",
            root_id=vulnerability_id,
        )

    @staticmethod
    def _validate_status_reason(status: str, reason: str) -> None:
        if status == "忽略" and len(reason.strip()) < 4:
            raise ConflictError("Ignored vulnerability requires a status reason")

    def _validate_artifacts(
        self,
        conn,
        *,
        task_id: str,
        finding_artifact_id: str | None,
        evidence_artifact_ids: list[str],
    ) -> None:
        if len(evidence_artifact_ids) != len(set(evidence_artifact_ids)):
            raise ConflictError("Evidence Artifact IDs must be unique")
        if finding_artifact_id and finding_artifact_id in evidence_artifact_ids:
            raise ConflictError("The Findings Artifact cannot also be Evidence")

        if finding_artifact_id:
            finding = self.artifacts.get(conn, task_id, finding_artifact_id)
            if finding is None:
                raise NotFoundError(f"Artifact not found: {finding_artifact_id}")
            if finding.artifact_type not in {
                "bootstrap.findings",
                "explore.findings",
                "upload",
            }:
                raise ConflictError("Finding Artifact must be a findings or upload Artifact")

        for artifact_id in evidence_artifact_ids:
            evidence = self.artifacts.get(conn, task_id, artifact_id)
            if evidence is None:
                raise NotFoundError(f"Artifact not found: {artifact_id}")
            if evidence.artifact_type not in {"explore.evidence", "upload"}:
                raise ConflictError("Evidence Artifact must be an evidence or upload Artifact")
