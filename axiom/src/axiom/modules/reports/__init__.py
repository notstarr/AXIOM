"""Report assembly: skeleton (code-owned layout) + attack path + evidence hook.

Explore submits content (structured reproduction steps), never layout.  The chapter
order, step numbering and empty-field placeholders live here so every report looks the
same no matter which worker wrote it.
"""

from __future__ import annotations

from sqlalchemy import Connection

from axiom.shared.errors import ConflictError, NotFoundError

from axiom.modules.reports.evidence import (
    KIND_BUSINESS,
    KIND_PACKET,
    KINDS,
    MAX_EVIDENCE_PER_SUBJECT,
    MAX_STEP_INDEX,
    SUBJECTS,
    SUBJECT_RESULT,
    SUBJECT_VULNERABILITY,
    ReportEvidenceRepository,
    missing_packet_steps,
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
    """Transaction wrapper around the evidence hook, plus the step-range rules.

    The API layer never opens a connection itself, and the two things a caller can get
    wrong — hanging a packet shot on a step that does not exist, or claiming a kind
    without saying which one — are rejected here rather than in each route.
    """

    def __init__(self, engine, *, vulnerabilities, red_team, artifacts) -> None:
        self.engine = engine
        self.vulnerabilities = vulnerabilities
        self.red_team = red_team
        self.artifacts = artifacts
        self.repository = ReportEvidenceRepository()

    def list(self, task_id: str, subject_type: str, subject_id: str) -> list[dict]:
        with self.engine.connect() as conn:
            return self.repository.list(conn, task_id, subject_type, subject_id)

    def missing_packet_steps(self, task_id: str, subject_type: str, subject_id: str) -> list[int]:
        """Steps of the stored reproduction with no packet shot.

        Empty when the subject has no structured reproduction: manual and historical
        findings have no steps to shoot, and must not be blocked by a rule about steps.
        """
        return missing_packet_steps(
            self._step_count(task_id, subject_type, subject_id),
            self.list(task_id, subject_type, subject_id),
        )

    def _step_count(self, task_id: str, subject_type: str, subject_id: str) -> int:
        reproduction = self._reproduction(task_id, subject_type, subject_id)
        return len(reproduction.steps) if reproduction is not None else 0

    def _reproduction(self, task_id: str, subject_type: str, subject_id: str):
        if subject_type == SUBJECT_VULNERABILITY:
            vulnerability = self.vulnerabilities.get(task_id, subject_id)
            if vulnerability is None:
                raise NotFoundError(f"Vulnerability not found: {subject_id}")
            return vulnerability.reproduction
        if subject_type == SUBJECT_RESULT:
            with self.engine.connect() as conn:
                result = self.red_team.repository.get_result(conn, task_id, subject_id)
            if result is None:
                raise NotFoundError(f"RedTeam Result not found: {subject_id}")
            return result.reproduction
        raise ConflictError(f"Unsupported report subject type: {subject_type}")

    def _check_step(self, *, task_id: str, subject_type: str, subject_id: str, step_index: int, kind: str) -> None:
        """Packet shots have to land on a real step; business shots keep the free range."""
        if step_index < 0:
            raise ConflictError("step_index must be 0 (overview) or a positive step number")
        if kind == KIND_PACKET:
            steps = self._step_count(task_id, subject_type, subject_id)
            if steps == 0:
                raise ConflictError(
                    "该对象没有结构化复现，不能挂报文图：报文图要挂在某一步的真实回包上。"
                )
            if not 1 <= step_index <= steps:
                raise ConflictError(
                    f"该对象有 {steps} 步复现，报文图的 step_index 只能是 1..{steps}；"
                    f"当前是 {step_index}"
                )
            return
        if step_index > MAX_STEP_INDEX:
            raise ConflictError(f"step_index 不能超过 {MAX_STEP_INDEX}")

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
        kind: str = KIND_BUSINESS,
    ) -> dict:
        if kind not in KINDS:
            raise ConflictError(f"不支持的截图种类：{kind}（只能是 packet 或 business）")
        self._check_step(
            task_id=task_id,
            subject_type=subject_type,
            subject_id=subject_id,
            step_index=step_index,
            kind=kind,
        )
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
                kind=kind,
            )

    def capture(
        self,
        *,
        task_id: str,
        subject_type: str,
        subject_id: str,
        filename: str,
        content: bytes,
        step_index: int,
        caption: str,
        created_by: str,
        kind: str = KIND_BUSINESS,
    ) -> dict:
        """Upload a picture and hang it on a step in one call.

        This is the entry point a capture-side tool codes against: it hands over only
        bytes, the step and the kind, and never has to know how artifacts or attachments
        are stored.  The step is validated *before* anything is written, and a failed
        attach archives the upload, so a rejected call leaves no orphan image.
        """
        if kind not in KINDS:
            raise ConflictError(f"不支持的截图种类：{kind}（只能是 packet 或 business）")
        self._check_step(
            task_id=task_id,
            subject_type=subject_type,
            subject_id=subject_id,
            step_index=step_index,
            kind=kind,
        )
        artifact = self.artifacts.upload(
            task_id,
            filename=filename,
            content=content,
            created_by=created_by,
            summary=caption or filename,
        )
        try:
            return self.attach(
                task_id=task_id,
                subject_type=subject_type,
                subject_id=subject_id,
                artifact_id=artifact.id,
                step_index=step_index,
                caption=caption,
                created_by=created_by,
                kind=kind,
            )
        except BaseException:
            try:
                self.artifacts.archive(task_id, artifact.id)
            except BaseException:  # pragma: no cover - best effort cleanup
                pass
            raise

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
    "KINDS",
    "KIND_BUSINESS",
    "KIND_PACKET",
    "MAX_EVIDENCE_PER_SUBJECT",
    "MAX_STEP_INDEX",
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
    "missing_packet_steps",
    "to_dict",
    "to_markdown",
]
