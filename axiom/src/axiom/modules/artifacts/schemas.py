from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from axiom.modules.vulnerabilities.schemas import VulnerabilityReport


class BootstrapBundle(BaseModel):
    asset_inventory: str
    initial_leads: str
    findings: str

    @field_validator("asset_inventory", "initial_leads", "findings")
    @classmethod
    def require_content(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Bootstrap artifact content must not be empty")
        return value + "\n"

    def files(self) -> dict[str, str]:
        return {
            "asset_inventory.md": self.asset_inventory,
            "initial_leads.md": self.initial_leads,
            "findings.md": self.findings,
        }


class ExploreArtifact(BaseModel):
    filename: str = Field(min_length=1, max_length=128)
    artifact_type: str = Field(min_length=1, max_length=128)
    summary: str = Field(default="", max_length=500)
    content: str = Field(min_length=1)

    @field_validator("filename", "artifact_type", "summary")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("filename")
    @classmethod
    def require_safe_filename(cls, value: str) -> str:
        if "/" in value or "\\" in value or value in {".", ".."}:
            raise ValueError("Explore artifact filename must be one safe file name")
        return value


class ExploreBundle(BaseModel):
    fact: str = Field(min_length=1, max_length=2_000)
    artifacts: list[ExploreArtifact] = Field(min_length=1, max_length=50)
    vulnerabilities: list[VulnerabilityReport] = Field(default_factory=list, max_length=20)

    @field_validator("fact")
    @classmethod
    def normalize_summary(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Explore Fact must not be empty")
        return value

    @field_validator("artifacts")
    @classmethod
    def unique_filenames(cls, values: list[ExploreArtifact]) -> list[ExploreArtifact]:
        names = [item.filename for item in values]
        if len(names) != len(set(names)):
            raise ValueError("Explore artifact filenames must be unique")
        return values


class Artifact(BaseModel):
    id: str
    task_id: str
    intent_id: str | None = None
    artifact_type: str
    storage_key: str
    summary: str
    content_hash: str
    size: int
    created_by: str
    created_at: str
    archived: bool = False


class ArtifactContent(BaseModel):
    artifact: Artifact
    content: str
