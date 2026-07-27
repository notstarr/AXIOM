from __future__ import annotations

from sqlalchemy import Connection, delete, select, update

from axiom.modules.credentials.schemas import (
    Credential,
    CredentialInput,
    CredentialRefresh,
    CredentialSummary,
)
from axiom.modules.credentials.tables import credential_refreshes, credentials
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
        credential_id = allocate_id(
            conn,
            scope="global",
            kind="credential",
            prefix="cred_",
        )
        conn.execute(
            credentials.insert().values(
                id=credential_id,
                project_id=project_id,
                name=command.name,
                content=command.content,
                notes=command.notes,
                enabled=command.enabled,
                created_at=now,
                updated_at=now,
            )
        )
        self._sync_refresh(
            conn,
            credential_id=credential_id,
            content=command.refresh_content,
            now=now,
        )
        created = self.get(conn, project_id, credential_id)
        assert created is not None
        return created

    def get(
        self,
        conn: Connection,
        project_id: str,
        credential_id: str,
    ) -> Credential | None:
        row = conn.execute(
            self._credential_select(include_content=True).where(
                credentials.c.project_id == project_id,
                credentials.c.id == credential_id,
            )
        ).mappings().one_or_none()
        return Credential.model_validate(row) if row is not None else None

    def get_summary(
        self,
        conn: Connection,
        project_id: str,
        credential_id: str,
    ) -> CredentialSummary | None:
        row = conn.execute(
            self._credential_select(include_content=False).where(
                credentials.c.project_id == project_id,
                credentials.c.id == credential_id,
            )
        ).mappings().one_or_none()
        return CredentialSummary.model_validate(row) if row is not None else None

    def list(
        self,
        conn: Connection,
        project_id: str,
        *,
        enabled_only: bool = False,
    ) -> list[CredentialSummary]:
        statement = self._credential_select(include_content=False).where(
            credentials.c.project_id == project_id
        )
        if enabled_only:
            statement = statement.where(credentials.c.enabled.is_(True))
        rows = conn.execute(
            statement.order_by(credentials.c.created_at, credentials.c.id)
        ).mappings()
        return [CredentialSummary.model_validate(row) for row in rows]

    def update(
        self,
        conn: Connection,
        project_id: str,
        credential_id: str,
        command: CredentialInput,
        *,
        now: str,
    ) -> Credential | None:
        result = conn.execute(
            update(credentials)
            .where(
                credentials.c.project_id == project_id,
                credentials.c.id == credential_id,
            )
            .values(
                name=command.name,
                content=command.content,
                notes=command.notes,
                enabled=command.enabled,
                updated_at=now,
            )
        )
        if not result.rowcount:
            return None
        self._sync_refresh(
            conn,
            credential_id=credential_id,
            content=command.refresh_content,
            now=now,
        )
        return self.get(conn, project_id, credential_id)

    def set_enabled(
        self,
        conn: Connection,
        project_id: str,
        credential_id: str,
        *,
        enabled: bool,
        now: str,
    ) -> CredentialSummary | None:
        result = conn.execute(
            update(credentials)
            .where(
                credentials.c.project_id == project_id,
                credentials.c.id == credential_id,
            )
            .values(enabled=enabled, updated_at=now)
        )
        return self.get_summary(conn, project_id, credential_id) if result.rowcount else None

    def get_refresh(
        self,
        conn: Connection,
        project_id: str,
        refresh_id: str,
        *,
        enabled_only: bool = False,
    ) -> CredentialRefresh | None:
        statement = (
            select(
                credential_refreshes.c.id,
                credential_refreshes.c.credential_id,
                credential_refreshes.c.content,
                credential_refreshes.c.created_at,
                credential_refreshes.c.updated_at,
            )
            .select_from(
                credential_refreshes.join(
                    credentials,
                    credentials.c.id == credential_refreshes.c.credential_id,
                )
            )
            .where(
                credentials.c.project_id == project_id,
                credential_refreshes.c.id == refresh_id,
            )
        )
        if enabled_only:
            statement = statement.where(credentials.c.enabled.is_(True))
        row = conn.execute(statement).mappings().one_or_none()
        return CredentialRefresh.model_validate(row) if row is not None else None

    def delete(self, conn: Connection, project_id: str, credential_id: str) -> bool:
        result = conn.execute(
            delete(credentials).where(
                credentials.c.project_id == project_id,
                credentials.c.id == credential_id,
            )
        )
        return bool(result.rowcount)

    @staticmethod
    def _credential_select(*, include_content: bool):
        columns = [
            credentials.c.id,
            credentials.c.project_id,
            credentials.c.name,
        ]
        if include_content:
            columns.append(credentials.c.content)
        columns.extend(
            [
                credentials.c.notes,
                credential_refreshes.c.id.label("refresh_id"),
                credentials.c.enabled,
                credentials.c.created_at,
                credentials.c.updated_at,
            ]
        )
        return select(*columns).select_from(
            credentials.outerjoin(
                credential_refreshes,
                credential_refreshes.c.credential_id == credentials.c.id,
            )
        )

    @staticmethod
    def _sync_refresh(
        conn: Connection,
        *,
        credential_id: str,
        content: str | None,
        now: str,
    ) -> None:
        existing = conn.execute(
            select(credential_refreshes.c.id).where(
                credential_refreshes.c.credential_id == credential_id
            )
        ).scalar_one_or_none()
        if content is None:
            if existing is not None:
                conn.execute(
                    delete(credential_refreshes).where(
                        credential_refreshes.c.id == existing
                    )
                )
            return
        if existing is not None:
            conn.execute(
                update(credential_refreshes)
                .where(credential_refreshes.c.id == existing)
                .values(content=content, updated_at=now)
            )
            return
        refresh_id = allocate_id(
            conn,
            scope="global",
            kind="credential_refresh",
            prefix="crf_",
        )
        conn.execute(
            credential_refreshes.insert().values(
                id=refresh_id,
                credential_id=credential_id,
                content=content,
                created_at=now,
                updated_at=now,
            )
        )
