"""Attach reviewer screenshots to a report after the fact.

Explore ships packets, not pictures.  A delivered report still usually wants the one or
two key screenshots, and those are the reviewer's to take — they have already replayed
the exploit to adjudicate it.  The images are stored as ordinary uploaded artifacts;
this layer just records *which* report, *which step* and *which kind* each one belongs
to, so the renderer can interleave them and the review gate can tell a packet shot from
a business shot.

The gate is why ``kind`` exists at all.  A picture of the admin table proves the outcome
but not the step that got there; only a response captured off the wire does.  So
``packet`` shots are required per step and ``business`` shots are not.
"""

from __future__ import annotations

from typing import Any, Iterable

from sqlalchemy import Connection, case, delete, select

from axiom.modules.artifacts.tables import artifacts
from axiom.modules.reports.tables import report_evidence
from axiom.shared.clock import utcnow
from axiom.shared.errors import ConflictError, NotFoundError


SUBJECT_VULNERABILITY = "vulnerability"
SUBJECT_RESULT = "redteam_result"
SUBJECTS = frozenset({SUBJECT_VULNERABILITY, SUBJECT_RESULT})

KIND_PACKET = "packet"
KIND_BUSINESS = "business"
KINDS = frozenset({KIND_PACKET, KIND_BUSINESS})

# Enough for 12 steps of packet shots plus the business pictures that make a report
# readable; a report with 40 pictures is a screenshot dump, not evidence.
MAX_EVIDENCE_PER_SUBJECT = 40

# Packet shots must land on a real step, so the upper bound is the reproduction length.
# Business shots keep the old free range: they are decoration, and historical rows were
# attached before a report had any notion of steps.
MAX_STEP_INDEX = 200


def missing_packet_steps(step_count: int, evidence: Iterable[dict[str, Any]]) -> list[int]:
    """Steps 1..``step_count`` that have no packet shot yet.

    Pure so the review service, the API and the tests all agree on what "covered" means:
    one packet shot anywhere in the step is enough, and extra business shots never count.
    """
    if step_count <= 0:
        return []
    covered = {
        int(item.get("step_index") or 0)
        for item in evidence
        if (item.get("kind") or KIND_BUSINESS) == KIND_PACKET
    }
    return [step for step in range(1, step_count + 1) if step not in covered]


class ReportEvidenceRepository:
    def list(self, conn: Connection, task_id: str, subject_type: str, subject_id: str) -> list[dict]:
        rows = conn.execute(
            select(
                report_evidence.c.artifact_id,
                report_evidence.c.step_index,
                report_evidence.c.kind,
                report_evidence.c.caption,
                report_evidence.c.display_order,
                artifacts.c.storage_key,
                artifacts.c.summary,
            )
            .join(
                artifacts,
                (artifacts.c.task_id == report_evidence.c.task_id)
                & (artifacts.c.id == report_evidence.c.artifact_id),
            )
            .where(
                report_evidence.c.task_id == task_id,
                report_evidence.c.subject_type == subject_type,
                report_evidence.c.subject_id == subject_id,
                artifacts.c.archived.is_(False),
            )
            # Packet shots first within a step: the wire response is what a reader checks,
            # the business picture only illustrates it. Sorting here (rather than in each
            # renderer) keeps the console, the skeleton and the export in the same order.
            .order_by(
                report_evidence.c.step_index,
                case((report_evidence.c.kind == KIND_PACKET, 0), else_=1),
                report_evidence.c.display_order,
            )
        ).mappings()
        return [
            {
                "artifact_id": row["artifact_id"],
                "step_index": int(row["step_index"] or 0),
                "kind": row["kind"] or KIND_BUSINESS,
                "caption": row["caption"] or "",
                "display_order": int(row["display_order"] or 0),
                "storage_key": row["storage_key"],
                # The console fetches the image through the generic artifact download,
                # so the payload carries the id it needs rather than a bespoke URL.
                "url": f"/api/v1/tasks/{task_id}/artifacts/{row['artifact_id']}/download",
            }
            for row in rows
        ]

    def attach(
        self,
        conn: Connection,
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
        if subject_type not in SUBJECTS:
            raise ConflictError(f"Unsupported report subject type: {subject_type}")
        if kind not in KINDS:
            raise ConflictError(f"不支持的截图种类：{kind}（只能是 packet 或 business）")
        if step_index < 0:
            raise ConflictError("step_index must be 0 (overview) or a positive step number")
        artifact = conn.execute(
            select(artifacts.c.id, artifacts.c.summary).where(
                artifacts.c.task_id == task_id,
                artifacts.c.id == artifact_id,
                artifacts.c.archived.is_(False),
            )
        ).mappings().one_or_none()
        if artifact is None:
            raise NotFoundError(f"Artifact not found: {artifact_id}")

        existing = self.list(conn, task_id, subject_type, subject_id)
        if len(existing) >= MAX_EVIDENCE_PER_SUBJECT:
            raise ConflictError(
                f"该报告已关联 {len(existing)} 张证据，上限 {MAX_EVIDENCE_PER_SUBJECT}；"
                "请先删除多余图片"
            )
        attached = next(
            (item for item in existing if item["artifact_id"] == artifact_id), None
        )
        if attached is not None:
            # Re-attaching is a no-op, not an error: the reviewer may have double-submitted
            # or re-shot the same step.  But changing the kind of an existing picture is a
            # real edit (a business shot re-labelled as the packet shot would move a step
            # across the gate), so it is applied rather than ignored.
            if attached["kind"] != kind:
                conn.execute(
                    report_evidence.update()
                    .where(
                        report_evidence.c.task_id == task_id,
                        report_evidence.c.subject_type == subject_type,
                        report_evidence.c.subject_id == subject_id,
                        report_evidence.c.artifact_id == artifact_id,
                    )
                    .values(kind=kind, caption=caption.strip() or attached["caption"])
                )
                return next(
                    item
                    for item in self.list(conn, task_id, subject_type, subject_id)
                    if item["artifact_id"] == artifact_id
                )
            return attached

        conn.execute(
            report_evidence.insert().values(
                task_id=task_id,
                subject_type=subject_type,
                subject_id=subject_id,
                artifact_id=artifact_id,
                step_index=step_index,
                kind=kind,
                caption=caption.strip(),
                display_order=len(existing),
                created_by=created_by,
                created_at=utcnow(),
            )
        )
        return next(
            item
            for item in self.list(conn, task_id, subject_type, subject_id)
            if item["artifact_id"] == artifact_id
        )

    def detach(self, conn: Connection, *, task_id: str, subject_type: str, subject_id: str, artifact_id: str) -> None:
        result = conn.execute(
            delete(report_evidence).where(
                report_evidence.c.task_id == task_id,
                report_evidence.c.subject_type == subject_type,
                report_evidence.c.subject_id == subject_id,
                report_evidence.c.artifact_id == artifact_id,
            )
        )
        if result.rowcount == 0:
            raise NotFoundError(f"Report evidence not found: {artifact_id}")

    def for_subjects(
        self,
        conn: Connection,
        task_id: str,
        subject_type: str,
        subject_ids: list[str],
    ) -> dict[str, list[dict]]:
        """Bulk read for a listing, so the console does not fan out per row."""
        if not subject_ids:
            return {}
        grouped: dict[str, list[dict]] = {}
        for subject_id in subject_ids:
            grouped[subject_id] = self.list(conn, task_id, subject_type, subject_id)
        return grouped
