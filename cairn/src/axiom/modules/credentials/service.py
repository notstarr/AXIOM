from __future__ import annotations

from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError

from axiom.modules.common.time import utcnow
from axiom.modules.credentials.repository import CredentialRepository
from axiom.modules.credentials.schemas import CreateCredential, Credential, UpdateCredential
from axiom.modules.projects.repository import ProjectRepository


class CredentialService:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.repository = CredentialRepository()
        self.projects = ProjectRepository()

    def create(self, project_id: str, body: CreateCredential) -> Credential:
        try:
            with self.engine.begin() as conn:
                if not self.projects.exists(conn, project_id):
                    raise ValueError(f"Project not found: {project_id}")
                return self.repository.create(conn, project_id, body, now=utcnow())
        except IntegrityError as exc:
            raise ValueError(f"Credential label already exists: {body.label}") from exc

    def get(self, credential_id: str) -> Credential:
        with self.engine.connect() as conn:
            result = self.repository.get(conn, credential_id)
        if result is None:
            raise ValueError(f"Credential not found: {credential_id}")
        return result

    def list(self, project_id: str, *, active_only: bool = True) -> list[Credential]:
        with self.engine.connect() as conn:
            return self.repository.list(conn, project_id, active_only=active_only)

    def update(self, credential_id: str, body: UpdateCredential) -> Credential:
        try:
            with self.engine.begin() as conn:
                credential = self.repository.update(conn, credential_id, body, now=utcnow())
                if credential is None:
                    raise ValueError(f"Credential not found: {credential_id}")
                return credential
        except IntegrityError as exc:
            raise ValueError("Credential label already exists") from exc

    def delete(self, credential_id: str) -> None:
        with self.engine.begin() as conn:
            if not self.repository.delete(conn, credential_id):
                raise ValueError(f"Credential not found: {credential_id}")
