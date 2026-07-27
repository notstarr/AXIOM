from __future__ import annotations

import mimetypes
from pathlib import PurePosixPath

from axiom.modules.artifacts.schemas import Artifact
from axiom.modules.external_reports.schemas import (
    ExternalArtifactEvidence,
    ExternalVulnerabilityPayload,
)
from axiom.modules.vulnerabilities.schemas import Vulnerability
from axiom.shared.storage import FileStore


_TEXT_LIMIT_BYTES = 200_000


class ExternalVulnerabilityAdapter:
    """Translate AXIOM's canonical vulnerability into the generic outbound JSON shape."""

    def __init__(self, store: FileStore):
        self.store = store

    def render(self, vulnerability: Vulnerability) -> ExternalVulnerabilityPayload:
        return ExternalVulnerabilityPayload(
            title=vulnerability.title,
            type=vulnerability.type,
            severity=vulnerability.severity,
            affected_target=vulnerability.affected_target,
            description=vulnerability.description,
            prerequisites=vulnerability.prerequisites,
            impact=vulnerability.impact,
            remediation=vulnerability.remediation,
            status=vulnerability.status,
            status_reason=vulnerability.status_reason,
            intent_id=vulnerability.intent_id,
            source=vulnerability.source,
            reported_by=vulnerability.reported_by,
            finding=(
                self._artifact(vulnerability.finding_artifact)
                if vulnerability.finding_artifact
                else None
            ),
            evidence=[self._artifact(item) for item in vulnerability.evidence_artifacts],
        )

    def _artifact(self, artifact: Artifact) -> ExternalArtifactEvidence:
        content: str | None = None
        if artifact.size <= _TEXT_LIMIT_BYTES:
            try:
                content = self.store.read_bytes(artifact.storage_key).decode("utf-8")
            except (OSError, UnicodeDecodeError):
                content = None
        name = PurePosixPath(artifact.storage_key).name
        return ExternalArtifactEvidence(
            id=artifact.id,
            name=name,
            artifact_type=artifact.artifact_type,
            media_type=mimetypes.guess_type(name)[0] or "application/octet-stream",
            summary=artifact.summary,
            size=artifact.size,
            sha256=artifact.content_hash,
            content=content,
        )
