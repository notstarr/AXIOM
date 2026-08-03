from __future__ import annotations

import json
from pathlib import Path

import click

from axiom.modules.token_usage.backfill import TokenUsageBackfiller
from axiom.shared.database import create_sqlite_engine, schema_is_ready


def register_token_usage_commands(main: click.Group) -> None:
    @main.command("token-usage-backfill")
    @click.option("--db-path", type=click.Path(path_type=Path), required=True)
    @click.option("--data-dir", type=click.Path(path_type=Path), required=True)
    @click.option("--task-id", multiple=True)
    @click.option("--dry-run", is_flag=True)
    def token_usage_backfill(
        db_path: Path,
        data_dir: Path,
        task_id: tuple[str, ...],
        dry_run: bool,
    ) -> None:
        """Recover durable token usage from Pi Session JSONL files."""

        engine = create_sqlite_engine(db_path.expanduser().resolve())
        try:
            if not schema_is_ready(engine):
                raise click.ClickException(
                    "database schema is not ready; run `axiom db-upgrade` first"
                )
            report = TokenUsageBackfiller(engine, data_dir).run(
                task_ids=set(task_id),
                dry_run=dry_run,
            )
        finally:
            engine.dispose()
        click.echo(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
