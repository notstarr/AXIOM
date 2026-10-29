"""Report evidence: the screenshots a reviewer attaches *after* the fact.

Explore no longer ships evidence files — it ships the HTTP packets inline on the
finding/Result (``reproduction_json``), which is what actually proves the claim.  What
packets cannot carry is a picture, and a delivered report usually wants the key one or
two.  Those are added later, by a reviewer, against a specific step of the
reproduction.

The table covers both subjects so the report renderer has one place to read: a
vulnerability and a red-team Result are the same shape here (an artifact, an optional
step index, a kind, a caption).

``kind`` separates the two things a reviewer photographs.  A packet shot is the step's
real response on the wire — it is evidence, and the review gate asks for one per step.
A business shot is the effect behind the finding (the admin page, the data, the shell);
it makes the report readable but proves nothing about a particular step, so it never
satisfies the gate.
"""

from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKeyConstraint,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
)

from axiom.modules.artifacts.tables import artifacts
from axiom.modules.tasks.tables import tasks
from axiom.shared.database import UTCDateTimeText, metadata


report_evidence = Table(
    "report_evidence",
    metadata,
    Column("task_id", String, nullable=False),
    # 'vulnerability' | 'redteam_result' — the subject id is a vXXX / xXXX scoped to
    # the task, so (task_id, subject_type, subject_id) identifies it without a
    # polymorphic foreign key.
    Column("subject_type", String, nullable=False),
    Column("subject_id", String, nullable=False),
    Column("artifact_id", String, nullable=False),
    # 0 = 概览图（挂在报告的证据节）；>0 = 该步骤的截图（挂在「步骤 N」卡片后面）。
    Column("step_index", Integer, nullable=False, default=0, server_default="0"),
    # 'packet' = 该步的真实报文回包（重放页/代理），'business' = 业务效果图。
    # 审核闸门只认 packet：有图不等于有证据，业务图证明「结果」而不证明「这一步」。
    Column("kind", String, nullable=False, default="business", server_default="business"),
    Column("caption", Text, nullable=False, default="", server_default=""),
    Column("display_order", Integer, nullable=False, default=0, server_default="0"),
    Column("created_by", String, nullable=False, default="", server_default=""),
    Column("created_at", UTCDateTimeText, nullable=False),
    PrimaryKeyConstraint("task_id", "subject_type", "subject_id", "artifact_id"),
    ForeignKeyConstraint(["task_id"], [tasks.c.id], ondelete="CASCADE"),
    ForeignKeyConstraint(
        ["task_id", "artifact_id"],
        [artifacts.c.task_id, artifacts.c.id],
        ondelete="CASCADE",
    ),
    CheckConstraint(
        "subject_type IN ('vulnerability','redteam_result')",
        name="report_evidence_subject_type",
    ),
    CheckConstraint("step_index >= 0", name="report_evidence_step_index"),
    CheckConstraint("kind IN ('packet','business')", name="report_evidence_kind"),
)

Index(
    "ix_report_evidence_subject",
    report_evidence.c.task_id,
    report_evidence.c.subject_type,
    report_evidence.c.subject_id,
)
