from __future__ import annotations

import json

from sqlalchemy import Connection, delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from axiom.modules.axiom_tools.schemas import AxiomTool, AxiomToolBinding
from axiom.modules.axiom_tools.tables import axiom_tools, worker_axiom_tool_bindings


class AxiomToolRepository:
    def upsert(self, conn: Connection, values: dict[str, object]) -> None:
        statement = sqlite_insert(axiom_tools).values(**values).on_conflict_do_update(
            index_elements=["key"],
            set_={
                key: value
                for key, value in values.items()
                if key not in {"key", "created_at", "enabled"}
            },
        )
        conn.execute(statement)

    def get_row(self, conn: Connection, key: str):
        return conn.execute(
            select(axiom_tools).where(axiom_tools.c.key == key)
        ).mappings().one_or_none()

    def list_rows(self, conn: Connection):
        return conn.execute(
            select(axiom_tools).order_by(axiom_tools.c.title, axiom_tools.c.key)
        ).mappings().all()

    def set_enabled(self, conn: Connection, key: str, enabled: bool, now: str) -> bool:
        return bool(
            conn.execute(
                axiom_tools.update()
                .where(axiom_tools.c.key == key)
                .values(enabled=enabled, updated_at=now)
            ).rowcount
        )

    def mark_missing(self, conn: Connection, keys: set[str], now: str) -> None:
        if not keys:
            return
        conn.execute(
            axiom_tools.update()
            .where(axiom_tools.c.key.in_(keys))
            .values(
                status="missing",
                status_error="工具包目录或清单在最近一次扫描中不存在",
                discovered_at=now,
                updated_at=now,
            )
        )

    def bindings(self, conn: Connection, *, worker_name: str | None = None, tool_key: str | None = None):
        statement = select(worker_axiom_tool_bindings)
        if worker_name is not None:
            statement = statement.where(worker_axiom_tool_bindings.c.worker_name == worker_name)
        if tool_key is not None:
            statement = statement.where(worker_axiom_tool_bindings.c.tool_key == tool_key)
        return conn.execute(
            statement.order_by(
                worker_axiom_tool_bindings.c.worker_name,
                worker_axiom_tool_bindings.c.phase,
                worker_axiom_tool_bindings.c.tool_key,
            )
        ).mappings().all()

    def replace_worker_bindings(
        self,
        conn: Connection,
        *,
        worker_name: str,
        bindings: dict[str, list[str]],
        now: str,
    ) -> None:
        conn.execute(
            delete(worker_axiom_tool_bindings).where(
                worker_axiom_tool_bindings.c.worker_name == worker_name
            )
        )
        rows = [
            {
                "worker_name": worker_name,
                "phase": phase,
                "tool_key": key,
                "created_at": now,
                "updated_at": now,
            }
            for phase, keys in bindings.items()
            for key in keys
        ]
        if rows:
            conn.execute(worker_axiom_tool_bindings.insert(), rows)

    def replace_tool_bindings(
        self,
        conn: Connection,
        *,
        tool_key: str,
        bindings: list[AxiomToolBinding],
        now: str,
    ) -> None:
        conn.execute(
            delete(worker_axiom_tool_bindings).where(
                worker_axiom_tool_bindings.c.tool_key == tool_key
            )
        )
        rows = [
            {
                "worker_name": binding.worker_name,
                "phase": binding.phase,
                "tool_key": tool_key,
                "created_at": now,
                "updated_at": now,
            }
            for binding in bindings
        ]
        if rows:
            conn.execute(worker_axiom_tool_bindings.insert(), rows)

    @staticmethod
    def model(row, *, bindings: list[AxiomToolBinding] | None = None) -> AxiomTool:
        return AxiomTool(
            key=row["key"],
            title=row["title"],
            version=row["version"],
            description=row["description"],
            source=row["source"],
            package_path=row["package_path"],
            entrypoint=row["entrypoint"],
            manifest=json.loads(row["manifest_json"] or "{}"),
            input_schema=json.loads(row["input_schema_json"] or "{}"),
            supported_phases=json.loads(row["supported_phases_json"] or "[]"),
            risk_tags=json.loads(row["risk_tags_json"] or "[]"),
            package_hash=row["package_hash"],
            status=row["status"],
            status_error=row["status_error"],
            enabled=bool(row["enabled"]),
            discovered_at=row["discovered_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            bindings=bindings or [],
        )
