from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from axiom.modules.artifacts.schemas import Artifact


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
        return (value or "").strip()


class ExploreVulnerabilityReport(VulnerabilityDetails):
    """Worker fields. Execution identity, status and Artifact IDs are injected by AXIOM."""

    prerequisites: str
    remediation: str

    evidence_paths: list[str] = Field(
        default_factory=list,
        max_length=MAX_VULNERABILITY_EVIDENCE_ARTIFACTS,
    )

    @field_validator("prerequisites", "remediation")
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
