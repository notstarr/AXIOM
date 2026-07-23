from __future__ import annotations

import json

from sqlalchemy import Connection, select, update

from axiom.modules.credentials.schemas import Credential, CredentialInput, CredentialMaterial
from axiom.modules.credentials.tables import credentials
from axiom.shared.ids import allocate_id


class CredentialRepository:
    def create(
        self,
        conn: Connection,
        *,
        project_id: str,
        command: CredentialInput,
        now: str,
    ) -> Credential:
        credential_id = allocate_id(conn, scope="global", kind="credential", prefix="cred_")
        values = {
            "id": credential_id,
            "project_id": project_id,
            "name": command.name,
            "credential_type": command.credential_type,
            "target_host": command.target_host,
            "data_json": json.dumps(command.data, ensure_ascii=False),
            "enabled": command.enabled,
            "created_at": now,
            "updated_at": now,
        }
        conn.execute(credentials.insert().values(**values))
        return self._model(values)

    def get(self, conn: Connection, project_id: str, credential_id: str) -> Credential | None:
        row = conn.execute(
            select(credentials).where(
                credentials.c.project_id == project_id,
                credentials.c.id == credential_id,
            )
        ).mappings().one_or_none()
        return self._model(row) if row is not None else None

    def list(self, conn: Connection, project_id: str) -> list[Credential]:
        rows = conn.execute(
            select(credentials)
            .where(credentials.c.project_id == project_id)
            .order_by(credentials.c.created_at, credentials.c.id)
        ).mappings()
        return [self._model(row) for row in rows]

    def update(
        self,
        conn: Connection,
        project_id: str,
        credential_id: str,
        command: CredentialInput,
        *,
        now: str,
    ) -> Credential | None:
        values = {
            "name": command.name,
            "credential_type": command.credential_type,
            "target_host": command.target_host,
            "data_json": json.dumps(command.data, ensure_ascii=False),
            "enabled": command.enabled,
            "updated_at": now,
        }
        result = conn.execute(
            update(credentials)
            .where(
                credentials.c.project_id == project_id,
                credentials.c.id == credential_id,
            )
            .values(**values)
        )
        return self.get(conn, project_id, credential_id) if result.rowcount else None

    def for_project_materials(self, conn: Connection, project_id: str) -> list[CredentialMaterial]:
        return [
            CredentialMaterial.model_validate(item.model_dump(mode="json"))
            for item in self.list(conn, project_id)
            if item.enabled
        ]

    @staticmethod
    def _model(row) -> Credential:
        values = dict(row)
        values["data"] = json.loads(values.pop("data_json") or "{}")
        return Credential.model_validate(values)
