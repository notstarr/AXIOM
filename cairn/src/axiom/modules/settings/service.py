from __future__ import annotations

import json

from sqlalchemy import Engine, select, update

from axiom.infrastructure.database.metadata import runtime_settings
from axiom.modules.common.time import utcnow
from axiom.modules.settings.schemas import RuntimeConfig, RuntimeSettings


class RuntimeSettingsService:
    SETTINGS_ID = "dispatcher"

    def __init__(self, engine: Engine):
        self.engine = engine

    def get(self) -> RuntimeSettings:
        with self.engine.begin() as conn:
            row = conn.execute(
                select(runtime_settings).where(runtime_settings.c.id == self.SETTINGS_ID)
            ).mappings().one_or_none()
            if row is None:
                now = utcnow()
                config = RuntimeConfig()
                row = {
                    "id": self.SETTINGS_ID,
                    "config_json": config.model_dump_json(),
                    "version": 1,
                    "updated_by": "system.default",
                    "updated_at": now,
                }
                conn.execute(runtime_settings.insert().values(**row))
            return self._model(row)

    def update(self, config: RuntimeConfig, *, actor: str) -> RuntimeSettings:
        actor = actor.strip()
        if not actor:
            raise ValueError("actor must not be empty")
        current = self.get()
        now = utcnow()
        with self.engine.begin() as conn:
            conn.execute(
                update(runtime_settings)
                .where(runtime_settings.c.id == self.SETTINGS_ID)
                .values(
                    config_json=config.model_dump_json(),
                    version=current.version + 1,
                    updated_by=actor,
                    updated_at=now,
                )
            )
            row = conn.execute(
                select(runtime_settings).where(runtime_settings.c.id == self.SETTINGS_ID)
            ).mappings().one()
            return self._model(row)

    @staticmethod
    def _model(row) -> RuntimeSettings:
        return RuntimeSettings(
            config=RuntimeConfig.model_validate_json(row["config_json"]),
            version=row["version"],
            updated_by=row["updated_by"],
            updated_at=row["updated_at"],
        )
