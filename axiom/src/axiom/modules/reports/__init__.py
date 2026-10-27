"""Report assembly: skeleton (code-owned layout) + attack path + evidence hook.

Explore submits content (structured reproduction steps), never layout.  The chapter
order, step numbering and empty-field placeholders live here so every report looks the
same no matter which worker wrote it.
"""

from __future__ import annotations

from sqlalchemy import Connection

from axiom.shared.errors import NotFoundError

from axiom.modules.reports.evidence import (
    MAX_EVIDENCE_PER_SUBJECT,
    SUBJECTS,
    SUBJECT_RESULT,
    SUBJECT_VULNERABILITY,
    ReportEvidenceRepository,
)
from axiom.modules.reports.path import PathLink, attack_path, attack_paths, load_graph
from axiom.modules.reports.skeleton import (
    EMPTY,
    NO_EVIDENCE,
    NO_REPRODUCTION,
    Block,
    ReportDoc,
    Section,
    build_result_report,
    build_vulnerability_report,
    to_dict,
    to_markdown,
)


class ReportService:
    """Assemble the human-facing report document for one subject.

    Two callers: the console (``to_dict``) and the export path (``to_markdown``).  The
    reviewer never comes through here — it reads the raw stored row.
    """

    def __init__(self, engine, *, vulnerabilities, red_team) -> None:
        self.engine = engine
        self.vulnerabilities = vulnerabilities
        self.red_team = red_team
        self.evidence = ReportEvidenceRepository()

    def vulnerability_doc(self, task_id: str, vulnerability_id: str) -> dict:
        # The service applies the task-visibility rules (archived tasks, etc.); the
        # report view must not be a way around them.
        vulnerability = self.vulnerabilities.get(task_id, vulnerability_id)
        with self.engine.connect() as conn:
            evidence = self.evidence.list(conn, task_id, SUBJECT_VULNERABILITY, vulnerability_id)
        return to_dict(build_vulnerability_report(vulnerability, evidence=evidence))

    def result_doc(self, task_id: str, result_id: str) -> dict:
        with self.engine.connect() as conn:
            result = self.red_team.repository.get_result(conn, task_id, result_id)
            if result is None:
                raise NotFoundError(f"RedTeam Result not found: {result_id}")
            evidence = self.evidence.list(conn, task_id, SUBJECT_RESULT, result_id)
            path = attack_path(
                conn,
                task_id=task_id,
                intent_id=result.intent_id,
                chain_id=result.chain_id,
            )
        return to_dict(build_result_report(result, attack_path=path, evidence=evidence))


class ReportEvidenceService:
    """Thin transaction wrapper so the API layer never opens a connection itself."""

    def __init__(self, engine) -> None:
        self.engine = engine
        self.repository = ReportEvidenceRepository()

    def list(self, task_id: str, subject_type: str, subject_id: str) -> list[dict]:
        with self.engine.connect() as conn:
            return self.repository.list(conn, task_id, subject_type, subject_id)

    def attach(
        self,
        *,
        task_id: str,
        subject_type: str,
        subject_id: str,
        artifact_id: str,
        step_index: int,
        caption: str,
        created_by: str,
    ) -> dict:
        with self.engine.begin() as conn:
            return self.repository.attach(
                conn,
                task_id=task_id,
                subject_type=subject_type,
                subject_id=subject_id,
                artifact_id=artifact_id,
                step_index=step_index,
                caption=caption,
                created_by=created_by,
            )

    def detach(self, *, task_id: str, subject_type: str, subject_id: str, artifact_id: str) -> None:
        with self.engine.begin() as conn:
            self.repository.detach(
                conn,
                task_id=task_id,
                subject_type=subject_type,
                subject_id=subject_id,
                artifact_id=artifact_id,
            )

    def for_subjects(self, conn: Connection, task_id: str, subject_type: str, subject_ids: list[str]) -> dict:
        return self.repository.for_subjects(conn, task_id, subject_type, subject_ids)


__all__ = [
    "EMPTY",
    "MAX_EVIDENCE_PER_SUBJECT",
    "NO_EVIDENCE",
    "NO_REPRODUCTION",
    "SUBJECTS",
    "SUBJECT_RESULT",
    "SUBJECT_VULNERABILITY",
    "Block",
    "PathLink",
    "ReportDoc",
    "ReportEvidenceRepository",
    "ReportEvidenceService",
    "ReportService",
    "Section",
    "attack_path",
    "attack_paths",
    "build_result_report",
    "build_vulnerability_report",
    "load_graph",
    "to_dict",
    "to_markdown",
]
