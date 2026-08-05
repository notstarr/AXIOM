from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path

import click
import uvicorn
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory

from axiom.cli_runner import register_runner_command
from axiom.modules.token_usage.cli import register_token_usage_commands
from axiom.shared.database import (
    AXIOM_SCHEMA,
    POSTGRESQL_ALEMBIC_BRANCH,
    create_database_engine,
    database_url_from_env,
    schema_is_ready,
)
from axiom.shared.maintenance import read_maintenance, write_maintenance
from axiom.skill_cli import register_skill_commands
from axiom.worker_cli import register_worker_commands


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@click.group()
def main() -> None:
    """AXIOM modular runtime."""


def database_url_option(function):
    return click.option(
        "--database-url",
        envvar="AXIOM_DATABASE_URL",
        required=False,
        help="PostgreSQL SQLAlchemy URL; defaults to AXIOM_DB_* environment variables.",
    )(function)


@main.group("db")
def db_commands() -> None:
    """PostgreSQL schema, migration and backup operations."""


@main.group("maintenance")
def maintenance_commands() -> None:
    """Control the shared drain/frozen maintenance state."""


@maintenance_commands.command("status")
@click.option("--data-dir", type=click.Path(path_type=Path), required=True)
def maintenance_status(data_dir: Path) -> None:
    click.echo(json.dumps(asdict(read_maintenance(data_dir)), ensure_ascii=False, indent=2))


@maintenance_commands.command("set")
@click.argument("state", type=click.Choice(["normal", "drain", "frozen"]))
@click.option("--data-dir", type=click.Path(path_type=Path), required=True)
@click.option("--actor", default="human.operator", show_default=True)
@click.option("--reason", default="")
def maintenance_set(
    state: str,
    data_dir: Path,
    actor: str,
    reason: str,
) -> None:
    status = write_maintenance(
        data_dir,
        state=state,  # type: ignore[arg-type]
        actor=actor,
        reason=reason,
    )
    click.echo(
        json.dumps(
            {
                "state": status.state,
                "actor": status.actor,
                "reason": status.reason,
                "updated_at": status.updated_at,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@db_commands.command("upgrade")
@database_url_option
def db_upgrade(database_url: str | None) -> None:
    """Apply the PostgreSQL Alembic branch."""

    _upgrade_database(database_url or database_url_from_env())
    click.echo(POSTGRESQL_ALEMBIC_BRANCH)


@main.command("db-upgrade", hidden=True)
@database_url_option
def db_upgrade_compat(database_url: str | None) -> None:
    """Compatibility alias for `axiom db upgrade`."""

    _upgrade_database(database_url or database_url_from_env())
    click.echo(POSTGRESQL_ALEMBIC_BRANCH)


@db_commands.command("migrate-sqlite")
@click.option("--sqlite-path", type=click.Path(path_type=Path), required=True)
@database_url_option
@click.option("--data-dir", type=click.Path(path_type=Path), required=False)
@click.option("--report-dir", type=click.Path(path_type=Path), required=False)
@click.option("--batch-size", type=click.IntRange(min=1), default=1_000, show_default=True)
def migrate_sqlite(
    sqlite_path: Path,
    database_url: str | None,
    data_dir: Path | None,
    report_dir: Path | None,
    batch_size: int,
) -> None:
    """Import a frozen AXIOM SQLite database into an empty PostgreSQL baseline."""

    from axiom.migration.sqlite_to_postgresql import migrate_sqlite_to_postgresql

    url = database_url or database_url_from_env()
    try:
        report = migrate_sqlite_to_postgresql(
            sqlite_path,
            url,
            data_dir=data_dir,
            batch_size=batch_size,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    output_dir = (
        report_dir.expanduser().resolve()
        if report_dir is not None
        else (
            data_dir.expanduser().resolve() / "postgresql-migration" / "reports"
            if data_dir is not None
            else Path.cwd() / "postgresql-migration-reports"
        )
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    report_id = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    json_path = output_dir / f"sqlite-to-postgresql-{report_id}.json"
    markdown_path = output_dir / f"sqlite-to-postgresql-{report_id}.md"
    json_path.write_text(
        json.dumps(report.as_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(report.markdown(), encoding="utf-8")
    click.echo(
        json.dumps(
            {
                "status": "ok",
                "json_report": str(json_path),
                "markdown_report": str(markdown_path),
                "summary": report.as_dict(),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@main.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8899, show_default=True, type=int)
@database_url_option
@click.option("--data-dir", type=click.Path(path_type=Path), required=True)
@click.option("--log-level", default="info", show_default=True)
def serve(
    host: str,
    port: int,
    database_url: str | None,
    data_dir: Path,
    log_level: str,
) -> None:
    """Start the AXIOM API after explicit PostgreSQL schema migration."""

    url = database_url or database_url_from_env()
    engine = create_database_engine(url, application_name="axiom-api")
    if not schema_is_ready(engine):
        raise click.ClickException("database schema is not ready; run `axiom db upgrade` first")
    current_heads = database_heads(engine)
    expected_head = migration_head(alembic_config(url))
    if current_heads != {expected_head}:
        current = ", ".join(sorted(current_heads)) or "none"
        raise click.ClickException(
            f"database migration is not current (current: {current}; expected: {expected_head}); "
            "run `axiom db upgrade` first"
        )
    from axiom.app import create_app

    uvicorn.run(
        create_app(engine=engine, data_dir=data_dir.expanduser().resolve()),
        host=host,
        port=port,
        log_level=log_level,
    )


def alembic_config(database_url: str) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def _upgrade_database(database_url: str) -> None:
    command.upgrade(alembic_config(database_url), POSTGRESQL_ALEMBIC_BRANCH)


def database_heads(engine) -> set[str]:
    with engine.connect() as connection:
        context = MigrationContext.configure(
            connection,
            opts={"version_table_schema": AXIOM_SCHEMA},
        )
        return set(context.get_current_heads())


def migration_head(config: Config) -> str:
    revision = ScriptDirectory.from_config(config).get_revision(POSTGRESQL_ALEMBIC_BRANCH)
    if revision is None:
        raise RuntimeError("PostgreSQL Alembic branch is missing")
    return revision.revision


register_runner_command(main)
register_skill_commands(main)
register_worker_commands(main)
register_token_usage_commands(main)
