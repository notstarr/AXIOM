import os
from pathlib import Path

import click
import uvicorn

from axiom.dispatcher.logging import configure_logging
from axiom.dispatcher.scheduler.loop import DispatcherLoop
from axiom.server import db


@click.group()
def main():
    """AXIOM - Fact-graph based collaborative exploration protocol."""


@main.command()
@click.option("--host", default="127.0.0.1", show_default=True, help="Bind host")
@click.option("--port", default=8888, show_default=True, help="Bind port")
@click.option(
    "--db-path",
    type=click.Path(path_type=Path),
    default="~/.local/share/axiom/axiom.db",
    show_default=True,
    help="SQLite database path",
)
@click.option(
    "--data-dir",
    type=click.Path(path_type=Path),
    default="~/.local/share/axiom",
    show_default=True,
)
@click.option("--log-level", default="info", show_default=True, help="Uvicorn log level")
@click.option("--access-log/--no-access-log", default=True, show_default=True, help="Enable Uvicorn access log")
def serve(host: str, port: int, db_path: Path, data_dir: Path, log_level: str, access_log: bool):
    """Start the AXIOM V2 API server."""
    from axiom.infrastructure.database.engine import create_sqlite_engine
    from axiom.server.v2_app import create_v2_app

    db_path = db_path.expanduser().resolve()
    data_dir = data_dir.expanduser().resolve()
    if not db_path.is_file():
        raise click.ClickException(
            f"V2 database not found: {db_path}. Run 'axiom migrate-v2' first."
        )
    application = create_v2_app(
        engine=create_sqlite_engine(db_path),
        data_dir=data_dir,
        service_token=os.getenv("AXIOM_SERVICE_TOKEN", ""),
    )

    uvicorn.run(
        application,
        host=host,
        port=port,
        log_level=log_level.lower(),
        access_log=access_log,
    )


@main.command("serve-legacy", hidden=True)
@click.option("--host", default="127.0.0.1")
@click.option("--port", default=8888)
@click.option("--db-path", type=click.Path(path_type=Path), default=str(db.DEFAULT_DB))
@click.option("--log-level", default="info")
def serve_legacy(host: str, port: int, db_path: Path, log_level: str):
    """Temporary rollback-only legacy server."""
    db.configure(db_path)
    from axiom.server.app import app

    uvicorn.run(app, host=host, port=port, log_level=log_level.lower())


@main.command("serve-v2")
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8889, show_default=True)
@click.option("--db-path", type=click.Path(path_type=Path), required=True)
@click.option("--data-dir", type=click.Path(path_type=Path), required=True)
@click.option("--log-level", default="info", show_default=True)
def serve_v2(host: str, port: int, db_path: Path, data_dir: Path, log_level: str):
    """Start the isolated AXIOM V2 API during migration development."""
    from axiom.infrastructure.database.engine import create_sqlite_engine
    from axiom.server.v2_app import create_v2_app

    application = create_v2_app(
        engine=create_sqlite_engine(db_path),
        data_dir=data_dir,
        service_token=os.getenv("AXIOM_SERVICE_TOKEN", ""),
    )
    uvicorn.run(application, host=host, port=port, log_level=log_level.lower())


@main.command("dispatch-legacy", hidden=True)
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Dispatcher config path",
)
@click.option("--once", is_flag=True, help="Run one scheduling iteration and exit")
@click.option(
    "--startup-healthcheck-only",
    is_flag=True,
    help="Run startup worker healthchecks and exit",
)
@click.option("--log-level", default="INFO", show_default=True, help="Log level")
def dispatch_legacy(config_path: Path, once: bool, startup_healthcheck_only: bool, log_level: str):
    """Run the AXIOM dispatcher."""
    configure_logging(log_level, bare=startup_healthcheck_only)
    loop = DispatcherLoop(config_path)
    try:
        if startup_healthcheck_only:
            loop.run_startup_healthchecks_only()
            return
        loop.run(once=once)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc


@main.command("dispatch")
@click.option("--server", default="http://127.0.0.1:8888", show_default=True)
@click.option("--service-token", default=lambda: os.getenv("AXIOM_SERVICE_TOKEN", ""))
@click.option("--once", is_flag=True)
@click.option("--log-level", default="INFO", show_default=True)
def dispatch(server: str, service_token: str, once: bool, log_level: str):
    """Run the database-backed V2 reconciliation dispatcher."""
    from axiom.dispatcher.v2.loop import V2DispatcherLoop

    configure_logging(log_level)
    if not service_token.strip():
        raise click.ClickException("AXIOM Service Token is not configured")
    V2DispatcherLoop(server=server, service_token=service_token).run(once=once)


@main.command("migrate-v2")
@click.option("--source", type=click.Path(path_type=Path, exists=True), required=True)
@click.option("--target", type=click.Path(path_type=Path), required=True)
@click.option("--data-dir", type=click.Path(path_type=Path), required=True)
@click.option("--mozzie-token-file", type=click.Path(path_type=Path))
def migrate_v2(source: Path, target: Path, data_dir: Path, mozzie_token_file: Path | None):
    """Migrate a stopped legacy database into the V2 schema."""
    from axiom.migration.v2 import migrate_legacy_database

    token = ""
    if mozzie_token_file and mozzie_token_file.is_file():
        token = mozzie_token_file.read_text(encoding="utf-8").strip()
    try:
        report = migrate_legacy_database(
            source,
            target,
            data_dir=data_dir,
            mozzie_token=token,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(report.model_dump_json(indent=2))


@main.command("crawler-supervisor")
@click.option("--project-id", required=True, help="Crawler project group id")
@click.option(
    "--db-path",
    type=click.Path(),
    default=str(db.DEFAULT_DB),
    show_default=True,
    help="SQLite database path",
)
def crawler_supervisor(project_id: str, db_path: str):
    """Run the phased crawler supervisor loop."""
    db.configure(Path(db_path))
    from axiom.server.routers.crawler import run_crawler_supervisor

    run_crawler_supervisor(project_id)
