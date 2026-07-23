from __future__ import annotations

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext

from axiom.cli import PROJECT_ROOT, alembic_config, database_heads, migration_heads
from axiom.shared import model_registry as _model_registry  # noqa: F401
from axiom.shared.database import create_sqlite_engine, metadata, schema_is_ready


def test_migrations_reach_head_without_metadata_drift(tmp_path) -> None:
    database_path = tmp_path / "migrated.db"
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{database_path}")

    command.upgrade(config, "head")

    engine = create_sqlite_engine(database_path)
    try:
        assert schema_is_ready(engine)
        assert database_heads(engine) == migration_heads(alembic_config(database_path))
        with engine.connect() as conn:
            context = MigrationContext.configure(
                conn,
                opts={"compare_type": True, "render_as_batch": True},
            )
            assert compare_metadata(context, metadata) == []
    finally:
        engine.dispose()
