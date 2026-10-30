from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Literal

import os

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from axiom.modules.artifacts.schemas import Artifact
from axiom.modules.reproduction.schemas import Reproduction


def reproduction_required() -> bool:
    """Whether a new Vulnerability must carry a structured reproduction.

    On by default: a finding with no replayable steps cannot be verified, cannot be
    captured, and reads as an assertion in the delivered report.  Manual entry is the
    case this deliberately closes — a hand-recorded finding needs the packets more than
    a worker does, because nobody else can retrace it.  ``curl`` input is accepted and
    translated, so the cost is one paste.

    Set it to ``0`` to reopen manual entry without packets for a transition period;
    what gets through stays visible as a report with no packets and no shots.
    """
    raw = os.environ.get("AXIOM_REQUIRE_REPRODUCTION", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


# A worker that has nothing to say sometimes writes the word for it.  "None" is not
# prose: rendered as-is it fills a whole chapter with the string a Python program uses
# for absence, and a reviewer cannot tell it apart from a real (if terse) statement.
_PLACEHOLDERS = {"", "none", "null", "undefined", "n/a", "na", "nil", "-", "—", "无", "暂无", "未填写"}


def normalize_optional_text(value: str | None) -> str:
    """Strip an optional prose field, and treat a written-out "nothing" as nothing."""
    text = (value or "").strip()
    return "" if text.lower() in _PLACEHOLDERS else text


Severity = Literal["critical", "high", "medium", "low", "info"]
VulnerabilityStatus = Literal["待验证", "已验证", "忽略", "重复"]
MAX_VULNERABILITY_EVIDENCE_ARTIFACTS = 20
_SAFE_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9_.-]+$")


class VulnerabilityDetails(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    type: str
    severity: Severity
    description: str
    prerequisites: str = ""
    impact: str
    remediation: str = ""
    affected_target: str = ""

    @field_validator("title", "type", "description", "impact")
    @classmethod
    def require_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    @field_validator("prerequisites", "remediation", "affected_target")
    @classmethod
    def normalize_optional(cls, value: str | None) -> str:
        return normalize_optional_text(value)


class ExploreVulnerabilityReport(VulnerabilityDetails):
    """Worker fields. Execution identity, status and Artifact IDs are injected by AXIOM."""

    prerequisites: str
    # 修复建议不再是 Explore 的必填项：本环节只负责证明漏洞成立，
    # 交付报告里的修复建议由 Review / AI 富化补。缺失时渲染器整节省略。
    remediation: str = ""
    # 结构化复现过程：入口 / 本环前置 / 有序真实报文 / 本环增量。
    # 这是 Explore 交付的核心内容——没有可复现路径就不是已确认漏洞。
    reproduction: Reproduction

    evidence_paths: list[str] = Field(
        default_factory=list,
        max_length=MAX_VULNERABILITY_EVIDENCE_ARTIFACTS,
    )

    @field_validator("prerequisites")
    @classmethod
    def require_explore_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    @field_validator("evidence_paths")
    @classmethod
    def validate_evidence_paths(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for raw in value:
            path = PurePosixPath(raw.strip())
            if path.parts and path.parts[0] in {"work", "artifacts"}:
                raise ValueError(
                    "证据路径必须是 evidence/<file>；work/ 只放不上报的原始记录，"
                    "不能作为漏洞证据"
                )
            if (
                path.is_absolute()
                or len(path.parts) < 2
                or path.parts[0] != "evidence"
                or ".." in path.parts
            ):
                raise ValueError("evidence path must be below evidence/")
            if any(
                not _SAFE_PATH_SEGMENT.fullmatch(segment) or segment.startswith(".")
                for segment in path.parts
            ):
                raise ValueError("evidence path contains an unsafe segment")
            candidate = path.as_posix()
            if candidate not in normalized:
                normalized.append(candidate)
        return normalized


class StoredExploreVulnerability(ExploreVulnerabilityReport):
    intent_id: str
    source: Literal["explore"] = "explore"
    reported_by: str

    @field_validator("intent_id", "reported_by")
    @classmethod
    def require_execution_identity(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value


class VulnerabilityReport(VulnerabilityDetails):
    intent_id: str | None = None
    source: str = "human"
    reported_by: str = "human"
    status: VulnerabilityStatus = "待验证"
    status_reason: str = ""
    finding_artifact_id: str | None = None
    # 复现过程：没有可复现路径的漏洞无法被审核者验证，也无法生成报文截图，交付报告
    # 里就只剩一句结论。默认必填；``AXIOM_REQUIRE_REPRODUCTION=0`` 可为过渡期重新放开
    # 人工无报文录入，与报文截图闸门用同一个开关思路。
    # 读取走 ``Vulnerability``，它的同名字段始终允许为空，以兼容本字段出现之前的行。
    reproduction: Reproduction | None = None

    @model_validator(mode="after")
    def require_reproduction_when_enforced(self) -> "VulnerabilityReport":
        if self.reproduction is None and reproduction_required():
            raise ValueError(
                "必须提供复现过程（reproduction）：写明入口、前置与真实收发报文。"
                "没有可复现路径的漏洞无法被审核，也无法生成报文截图。"
                "报文可以直接粘贴 curl 命令，会自动转换。"
            )
        return self
    evidence_artifact_ids: list[str] = Field(
        default_factory=list,
        max_length=MAX_VULNERABILITY_EVIDENCE_ARTIFACTS,
    )

    @field_validator("source", "reported_by")
    @classmethod
    def require_actor_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    @field_validator("intent_id", "finding_artifact_id")
    @classmethod
    def normalize_optional_id(cls, value: str | None) -> str | None:
        normalized = (value or "").strip()
        return normalized or None

    @field_validator("status_reason")
    @classmethod
    def normalize_status_reason(cls, value: str | None) -> str:
        return (value or "").strip()

    @field_validator("evidence_artifact_ids")
    @classmethod
    def normalize_artifact_ids(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip() for item in value if item.strip()))


class LegacyVulnerabilityDetails(BaseModel):
    reproduction_steps: list[str] = Field(default_factory=list)
    curl_command: str = ""
    evidence: list[str] = Field(default_factory=list)


class Vulnerability(VulnerabilityReport):
    id: str
    task_id: str
    format_version: int = 2
    # 审核者事后补充的证据图（report_evidence）：step_index 决定挂在报告的哪一步。
    evidence: list[dict] = Field(default_factory=list)
    finding_artifact: Artifact | None = None
    evidence_artifacts: list[Artifact] = Field(default_factory=list)
    legacy: LegacyVulnerabilityDetails | None = None
    created_at: str
    updated_at: str
    archived: bool = False
    human_reviewed: bool = False
    human_reviewed_at: str | None = None
    human_reviewed_by: str | None = None
    red_team_exploitable: bool = False
    red_team_exploitable_at: str | None = None
    red_team_exploitable_by: str | None = None


class SetVulnerabilityHumanReviewed(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: bool
    actor: str = "human.frontend"

    @field_validator("actor")
    @classmethod
    def require_actor_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("actor must not be empty")
        return value


class SetVulnerabilityRedTeamExploitable(SetVulnerabilityHumanReviewed):
    """人工「红队可利用」标记，与已阅开关同构。"""


class UpdateVulnerability(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    type: str | None = None
    severity: Severity | None = None
    description: str | None = None
    prerequisites: str | None = None
    impact: str | None = None
    remediation: str | None = None
    affected_target: str | None = None
    status: VulnerabilityStatus | None = None
    status_reason: str | None = None
    finding_artifact_id: str | None = None
    evidence_artifact_ids: list[str] | None = Field(
        default=None,
        max_length=MAX_VULNERABILITY_EVIDENCE_ARTIFACTS,
    )

    @field_validator("title", "type", "description", "impact")
    @classmethod
    def normalize_required(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    @field_validator(
        "prerequisites",
        "remediation",
        "affected_target",
        "status_reason",
    )
    @classmethod
    def normalize_optional(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @field_validator("finding_artifact_id")
    @classmethod
    def normalize_finding_id(cls, value: str | None) -> str | None:
        normalized = (value or "").strip()
        return normalized or None

    @field_validator("evidence_artifact_ids")
    @classmethod
    def normalize_evidence_ids(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return list(dict.fromkeys(item.strip() for item in value if item.strip()))
