from __future__ import annotations

from sqlalchemy import Engine, select, update

from axiom.modules.settings.schemas import RuntimeConfig, RuntimeSettings
from axiom.modules.settings.tables import runtime_settings
from axiom.shared.clock import utcnow
from axiom.shared.database import immediate_transaction
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
        with immediate_transaction(self.engine) as conn:
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
                }
                conn.execute(runtime_settings.insert().values(**row))
            return self._model(row)

    def update(self, config: RuntimeConfig, *, actor: str) -> RuntimeSettings:
        actor = actor.strip()
        if not actor:
            raise ConflictError("actor must not be empty")
        now = utcnow()
        with immediate_transaction(self.engine) as conn:
            row = conn.execute(
                select(runtime_settings).where(runtime_settings.c.id == self.SETTINGS_ID)
            ).mappings().one_or_none()
            if row is None:
                values = {
                    "id": self.SETTINGS_ID,
                    "config_json": config.model_dump_json(),
                    "version": 1,
                    "updated_by": actor,
                    "updated_at": now,
                }
                conn.execute(runtime_settings.insert().values(**values))
                return self._model(values)
            conn.execute(
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
            return RuntimeSettings(
                config=config,
                version=row["version"] + 1,
                updated_by=actor,
                updated_at=now,
            )

    @staticmethod
    def _model(row) -> RuntimeSettings:
        return RuntimeSettings(
            config=RuntimeConfig.model_validate_json(row["config_json"]),
            version=row["version"],
            updated_by=row["updated_by"],
            updated_at=row["updated_at"],
        )
