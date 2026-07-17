from __future__ import annotations

from axiom.infrastructure.database.engine import create_sqlite_engine
from axiom.infrastructure.database.metadata import metadata
from axiom.modules.settings.schemas import RuntimeConfig
from axiom.modules.settings.service import RuntimeSettingsService


def test_runtime_settings_are_database_backed_and_versioned(tmp_path) -> None:
    engine = create_sqlite_engine(tmp_path / "axiom-v2.db")
    metadata.create_all(engine)
    service = RuntimeSettingsService(engine)

    initial = service.get()
    assert initial.config.interval_seconds == 1
    assert initial.config.max_workers == 4
    assert initial.version == 1

    updated = service.update(
        RuntimeConfig(
            interval_seconds=2,
            max_workers=6,
            max_running_tasks=3,
            max_task_workers=3,
            bootstrap_limit=1,
            reason_limit=1,
            explore_limit=4,
        ),
        actor="human",
    )
    assert updated.version == 2
    assert RuntimeSettingsService(engine).get().config.max_workers == 6
