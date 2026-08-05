from __future__ import annotations

import json
from pathlib import Path

import click

from axiom.modules.token_usage.backfill import TokenUsageBackfiller
from axiom.shared.database import create_database_engine, database_url_from_env, schema_is_ready


def register_token_usage_commands(main: click.Group) -> None:
    @main.command("token-usage-backfill")
    @click.option("--database-url", envvar="AXIOM_DATABASE_URL", required=False)
    @click.option("--data-dir", type=click.Path(path_type=Path), required=True)
    @click.option("--task-id", multiple=True)
    @click.option("--dry-run", is_flag=True)
    def token_usage_backfill(
        database_url: str | None,
        data_dir: Path,
        task_id: tuple[str, ...],
        dry_run: bool,
    ) -> None:
        """Recover durable token usage from Pi Session JSONL files."""

        engine = create_database_engine(
            database_url or database_url_from_env(),
            application_name="axiom-token-backfill",
        )
        try:
            if not schema_is_ready(engine):
                raise click.ClickException(
                    "database schema is not ready; run `axiom db upgrade` first"
                )
            report = TokenUsageBackfiller(engine, data_dir).run(
                task_ids=set(task_id),
                dry_run=dry_run,
            )
        finally:
            engine.dispose()
        click.echo(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
