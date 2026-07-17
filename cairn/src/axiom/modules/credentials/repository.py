from __future__ import annotations

import json

from sqlalchemy import Connection, select

from axiom.infrastructure.database.metadata import credentials
from axiom.modules.common.ids import allocate_readable_id
from axiom.modules.credentials.schemas import CreateCredential, Credential


class CredentialRepository:
    def create(self, conn: Connection, project_id: str, body: CreateCredential, *, now: str) -> Credential:
        credential_id = allocate_readable_id(
            conn,
            scope="global",
            kind="credential",
            prefix="cred_",
            width=6,
        )
        values = {
            "id": credential_id,
            "project_id": project_id,
            "label": body.label,
            "auth_type": body.auth_type,
            "credentials_json": json.dumps(body.data, ensure_ascii=False),
            "domains_json": json.dumps(body.domains, ensure_ascii=False),
            "notes": body.notes,
            "active": True,
            "created_at": now,
            "updated_at": now,
        }
        conn.execute(credentials.insert().values(**values))
        return self._to_model(values)

    def get(self, conn: Connection, credential_id: str) -> Credential | None:
        row = conn.execute(
            select(credentials).where(credentials.c.id == credential_id)
        ).mappings().one_or_none()
        return self._to_model(row) if row is not None else None

    def list(self, conn: Connection, project_id: str, *, active_only: bool = True) -> list[Credential]:
        query = select(credentials).where(credentials.c.project_id == project_id)
        if active_only:
            query = query.where(credentials.c.active.is_(True))
        rows = conn.execute(query.order_by(credentials.c.label, credentials.c.id)).mappings()
        return [self._to_model(row) for row in rows]

    @staticmethod
    def _to_model(row) -> Credential:
        return Credential(
            id=row["id"],
            project_id=row["project_id"],
            label=row["label"],
            auth_type=row["auth_type"],
            data=json.loads(row["credentials_json"]),
            domains=json.loads(row["domains_json"] or "[]"),
            notes=row["notes"],
            active=bool(row["active"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
