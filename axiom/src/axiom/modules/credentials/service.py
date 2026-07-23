from __future__ import annotations

from sqlalchemy import Engine, delete

from axiom.modules.credentials.repository import CredentialRepository
from axiom.modules.credentials.schemas import Credential, CredentialInput
from axiom.modules.credentials.tables import credentials
from axiom.modules.projects.repository import ProjectRepository
from axiom.shared.clock import utcnow
from axiom.shared.errors import NotFoundError


class CredentialService:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.repository = CredentialRepository()
        self.projects = ProjectRepository()

    def create(self, project_id: str, command: CredentialInput) -> Credential:
        with self.engine.begin() as conn:
            if not self.projects.get(conn, project_id):
                raise NotFoundError(f"Project not found: {project_id}")
            return self.repository.create(
                conn,
                project_id=project_id,
                command=command,
                now=utcnow(),
            )

    def list(self, project_id: str) -> list[Credential]:
        with self.engine.connect() as conn:
            if not self.projects.get(conn, project_id):
                raise NotFoundError(f"Project not found: {project_id}")
            return self.repository.list(conn, project_id)

    def get(self, project_id: str, credential_id: str) -> Credential:
        with self.engine.connect() as conn:
            credential = self.repository.get(conn, project_id, credential_id)
        if credential is None:
            raise NotFoundError(f"Credential not found: {credential_id}")
        return credential

    def update(self, project_id: str, credential_id: str, command: CredentialInput) -> Credential:
        with self.engine.begin() as conn:
            updated = self.repository.update(
                conn,
                project_id,
                credential_id,
                command,
                now=utcnow(),
            )
        if updated is None:
            raise NotFoundError(f"Credential not found: {credential_id}")
        return updated

    def delete(self, project_id: str, credential_id: str) -> None:
        with self.engine.begin() as conn:
            result = conn.execute(
                delete(credentials).where(
                    credentials.c.project_id == project_id,
                    credentials.c.id == credential_id,
                )
            )
        if not result.rowcount:
            raise NotFoundError(f"Credential not found: {credential_id}")
