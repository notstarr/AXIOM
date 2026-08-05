from __future__ import annotations

from sqlalchemy import Engine, select, update

from axiom.modules.settings.schemas import RuntimeConfig, RuntimeSettings
from axiom.modules.settings.tables import runtime_settings
from axiom.shared.clock import utcnow
from axiom.shared.database import transaction
from axiom.shared.errors import ConflictError


def load_runtime_config(conn) -> RuntimeConfig:
    """Read settings inside an existing transaction without opening a nested write lock."""

    row = conn.execute(
        select(runtime_settings.c.config_json).where(runtime_settings.c.id == "runtime")
    ).scalar_one_or_none()
    return RuntimeConfig.model_validate_json(row) if row else RuntimeConfig()


class RuntimeSettingsService:
    SETTINGS_ID = "runtime"

    def __init__(self, engine: Engine):
        self.engine = engine

    def get(self) -> RuntimeSettings:
        with transaction(self.engine) as conn:
            row = conn.execute(
                select(runtime_settings).where(runtime_settings.c.id == self.SETTINGS_ID)
            ).mappings().one_or_none()
            if row is None:
                row = {
                    "id": self.SETTINGS_ID,
                    "config_json": RuntimeConfig().model_dump_json(),
                    "version": 1,
                    "updated_by": "system.default",
                    "updated_at": utcnow(),
                    "applied_version": 0,
                    "applied_at": None,
                }
                conn.execute(runtime_settings.insert().values(**row))
            return self._model(row)

    def update(
        self,
        config: RuntimeConfig,
        *,
        actor: str,
        expected_version: int,
    ) -> RuntimeSettings:
        actor = actor.strip()
        if not actor:
            raise ConflictError("actor must not be empty")
        now = utcnow()
        with transaction(self.engine) as conn:
            row = conn.execute(
                select(runtime_settings).where(runtime_settings.c.id == self.SETTINGS_ID)
            ).mappings().one_or_none()
            if row is None:
                if expected_version != 1:
                    raise ConflictError("Runtime settings version conflict")
                values = {
                    "id": self.SETTINGS_ID,
                    "config_json": config.model_dump_json(),
                    "version": 1,
                    "updated_by": actor,
                    "updated_at": now,
                    "applied_version": 0,
                    "applied_at": None,
                }
                conn.execute(runtime_settings.insert().values(**values))
                return self._model(values)
            if row["version"] != expected_version:
                raise ConflictError(
                    f"Runtime settings version conflict: expected {expected_version}, "
                    f"current {row['version']}"
                )
            result = conn.execute(
                update(runtime_settings)
                .where(
                    runtime_settings.c.id == self.SETTINGS_ID,
                    runtime_settings.c.version == row["version"],
                )
                .values(
                    config_json=config.model_dump_json(),
                    version=row["version"] + 1,
                    updated_by=actor,
                    updated_at=now,
                )
            )
            if not result.rowcount:
                raise ConflictError("Runtime settings update lost a concurrent race")
            return RuntimeSettings(
                config=config,
                version=row["version"] + 1,
                updated_by=actor,
                updated_at=now,
                applied_version=row["applied_version"],
                applied_at=row["applied_at"],
            )

    def mark_applied(self, version: int) -> RuntimeSettings:
        now = utcnow()
        with transaction(self.engine) as conn:
            row = conn.execute(
                select(runtime_settings).where(runtime_settings.c.id == self.SETTINGS_ID)
            ).mappings().one_or_none()
            if row is None:
                raise ConflictError("Runtime settings do not exist")
            if version > row["version"]:
                raise ConflictError(
                    f"Cannot apply unknown Runtime settings version {version}; "
                    f"current is {row['version']}"
                )
            if version > row["applied_version"]:
                conn.execute(
                    update(runtime_settings)
                    .where(runtime_settings.c.id == self.SETTINGS_ID)
                    .values(applied_version=version, applied_at=now)
                )
                row = {**row, "applied_version": version, "applied_at": now}
            return self._model(row)

    @staticmethod
    def _model(row) -> RuntimeSettings:
        return RuntimeSettings(
            config=RuntimeConfig.model_validate_json(row["config_json"]),
            version=row["version"],
            updated_by=row["updated_by"],
            updated_at=row["updated_at"],
            applied_version=row.get("applied_version", 0),
            applied_at=row.get("applied_at"),
        )
