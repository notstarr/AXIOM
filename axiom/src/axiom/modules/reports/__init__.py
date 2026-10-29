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
from axiom.modules.reports.skeleton import (
    EMPTY,
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

    def _images_by_step(self, conn, task_id: str, subject_type: str, subject_id: str) -> dict:
        """Group attached pictures by the step they belong to.

        The page reads ``evidence`` (a flat list with ``step_index``) so it can render
        whatever arrives; the report skeleton wants them pre-grouped, because its job is
        to place each picture under its step.
        """
        grouped: dict[int, list[dict]] = {}
        for item in self.evidence.list(conn, task_id, subject_type, subject_id):
            grouped.setdefault(int(item.get("step_index") or 0), []).append(item)
        return grouped

    def vulnerability_doc(self, task_id: str, vulnerability_id: str) -> dict:
        # The service applies the task-visibility rules (archived tasks, etc.); the
        # report view must not be a way around them.
        vulnerability = self.vulnerabilities.get(task_id, vulnerability_id)
        with self.engine.connect() as conn:
            images = self._images_by_step(conn, task_id, SUBJECT_VULNERABILITY, vulnerability_id)
        return to_dict(build_vulnerability_report(vulnerability, images_by_step=images))

    def result_doc(self, task_id: str, result_id: str) -> dict:
        with self.engine.connect() as conn:
            result = self.red_team.repository.get_result(conn, task_id, result_id)
            if result is None:
                raise NotFoundError(f"RedTeam Result not found: {result_id}")
            images = self._images_by_step(conn, task_id, SUBJECT_RESULT, result_id)
        return to_dict(build_result_report(result, images_by_step=images))


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
    "NO_REPRODUCTION",
    "SUBJECTS",
    "SUBJECT_RESULT",
    "SUBJECT_VULNERABILITY",
    "Block",
    "ReportDoc",
    "ReportEvidenceRepository",
    "ReportEvidenceService",
    "ReportService",
    "Section",
    "build_result_report",
    "build_vulnerability_report",
    "to_dict",
    "to_markdown",
]
