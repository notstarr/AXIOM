from __future__ import annotations

import json
from pathlib import Path

import click
import uvicorn
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory

from axiom.cli_runner import register_runner_command
from axiom.skill_cli import register_skill_commands
from axiom.worker_cli import register_worker_commands
from axiom.modules.token_usage.cli import register_token_usage_commands
from axiom.shared.database import create_sqlite_engine, schema_is_ready


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@click.group()
def main() -> None:
    """AXIOM modular runtime."""


@main.command("db-upgrade")
@click.option("--db-path", type=click.Path(path_type=Path), required=True)
def db_upgrade(db_path: Path) -> None:
    """Apply all database migrations."""
    database_path = db_path.expanduser().resolve()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    config = alembic_config(database_path)
    command.upgrade(config, "head")
    click.echo(str(database_path))


@main.command("migrate-legacy")
@click.option("--source-db", type=click.Path(path_type=Path), required=True)
@click.option("--target-db", type=click.Path(path_type=Path), required=True)
@click.option("--source-data-dir", type=click.Path(path_type=Path), required=True)
@click.option("--target-data-dir", type=click.Path(path_type=Path), required=True)
@click.option("--report-path", type=click.Path(path_type=Path), required=False)
def migrate_legacy(
    source_db: Path,
    target_db: Path,
    source_data_dir: Path,
    target_data_dir: Path,
    report_path: Path | None,
) -> None:
    """Migrate a frozen Cairn database into a new AXIOM database."""
    from axiom.migration.legacy import migrate_legacy_database

    try:
        report = migrate_legacy_database(
            source_db,
            target_db,
            source_data_dir=source_data_dir,
            target_data_dir=target_data_dir,
        )
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    payload = json.dumps(report.as_dict(), ensure_ascii=False, indent=2)
    if report_path is not None:
        report_path = report_path.expanduser().resolve()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(payload + "\n", encoding="utf-8")
    click.echo(payload)


@main.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8899, show_default=True, type=int)
@click.option("--db-path", type=click.Path(path_type=Path), required=True)
@click.option("--data-dir", type=click.Path(path_type=Path), required=True)
@click.option("--log-level", default="info", show_default=True)
def serve(host: str, port: int, db_path: Path, data_dir: Path, log_level: str) -> None:
    """Start the AXIOM API after explicit schema migration."""
    database_path = db_path.expanduser().resolve()
    engine = create_sqlite_engine(database_path)
    if not schema_is_ready(engine):
        raise click.ClickException("database schema is not ready; run `axiom db-upgrade` first")
    current_heads = database_heads(engine)
    expected_heads = migration_heads(alembic_config(database_path))
    if current_heads != expected_heads:
        current = ", ".join(sorted(current_heads)) or "none"
        expected = ", ".join(sorted(expected_heads)) or "none"
        raise click.ClickException(
            f"database migration is not current (current: {current}; expected: {expected}); "
            "run `axiom db-upgrade` first"
        )
    from axiom.app import create_app

    uvicorn.run(
        create_app(engine=engine, data_dir=data_dir.expanduser().resolve()),
        host=host,
        port=port,
        log_level=log_level,
    )


def alembic_config(database_path: Path) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{database_path}")
    return config


def database_heads(engine) -> set[str]:
    with engine.connect() as connection:
        return set(MigrationContext.configure(connection).get_current_heads())


def migration_heads(config: Config) -> set[str]:
    return set(ScriptDirectory.from_config(config).get_heads())


register_runner_command(main)
register_skill_commands(main)
register_worker_commands(main)
register_token_usage_commands(main)
