from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool, text

from axiom.shared import model_registry as _model_registry  # noqa: F401
from axiom.shared.database import AXIOM_SCHEMA, metadata


config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=True,
        version_table_schema=AXIOM_SCHEMA,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section) or {},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        if connection.dialect.name != "postgresql":
            raise RuntimeError("AXIOM Alembic runtime only supports PostgreSQL")
        connection.execute(text("SET ROLE axiom_owner"))
        connection.execute(
            text(f"CREATE SCHEMA IF NOT EXISTS {AXIOM_SCHEMA} AUTHORIZATION axiom_owner")
        )
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=metadata,
            include_schemas=True,
            version_table_schema=AXIOM_SCHEMA,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
