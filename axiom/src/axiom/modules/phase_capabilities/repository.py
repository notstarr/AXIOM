from __future__ import annotations

from sqlalchemy import Connection, delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from axiom.modules.phase_capabilities.tables import phase_capability_bindings


class PhaseCapabilityRepository:
    def list_rows(
        self,
        conn: Connection,
        *,
        scope: str | None = None,
        capability_kind: str | None = None,
        capability_key: str | None = None,
    ):
        statement = select(phase_capability_bindings)
        if scope is not None:
            statement = statement.where(phase_capability_bindings.c.scope == scope)
        if capability_kind is not None:
            statement = statement.where(
                phase_capability_bindings.c.capability_kind == capability_kind
            )
        if capability_key is not None:
            statement = statement.where(
                phase_capability_bindings.c.capability_key == capability_key
            )
        return conn.execute(
            statement.order_by(
                phase_capability_bindings.c.scope,
                phase_capability_bindings.c.capability_kind,
                phase_capability_bindings.c.capability_key,
            )
        ).mappings().all()

    def ensure(
        self,
        conn: Connection,
        *,
        scope: str,
        capability_kind: str,
        capability_key: str,
        now: str,
    ) -> None:
        conn.execute(
            pg_insert(phase_capability_bindings)
            .values(
                scope=scope,
                capability_kind=capability_kind,
                capability_key=capability_key,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(
                index_elements=["scope", "capability_kind", "capability_key"]
            )
        )

    def replace_capability(
        self,
        conn: Connection,
        *,
        capability_kind: str,
        capability_key: str,
        scopes: list[str],
        now: str,
    ) -> None:
        conn.execute(
            delete(phase_capability_bindings).where(
                phase_capability_bindings.c.capability_kind == capability_kind,
                phase_capability_bindings.c.capability_key == capability_key,
            )
        )
        if scopes:
            conn.execute(
                phase_capability_bindings.insert(),
                [
                    {
                        "scope": scope,
                        "capability_kind": capability_kind,
                        "capability_key": capability_key,
                        "created_at": now,
                        "updated_at": now,
                    }
                    for scope in scopes
                ],
            )

    def replace_scope(
        self,
        conn: Connection,
        *,
        scope: str,
        capability_kind: str,
        capability_keys: list[str],
        now: str,
    ) -> None:
        conn.execute(
            delete(phase_capability_bindings).where(
                phase_capability_bindings.c.scope == scope,
                phase_capability_bindings.c.capability_kind == capability_kind,
            )
        )
        if capability_keys:
            conn.execute(
                phase_capability_bindings.insert(),
                [
                    {
                        "scope": scope,
                        "capability_kind": capability_kind,
                        "capability_key": capability_key,
                        "created_at": now,
                        "updated_at": now,
                    }
                    for capability_key in capability_keys
                ],
            )
