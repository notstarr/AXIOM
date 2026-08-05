#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from sqlalchemy import select, text

from axiom.migration.sqlite_to_postgresql import _rows_hash
from axiom.shared import model_registry as _model_registry  # noqa: F401
from axiom.shared.database import AXIOM_SCHEMA, create_database_engine, metadata


def main() -> None:
    parser = argparse.ArgumentParser(description="Fingerprint every AXIOM PostgreSQL business table")
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    engine = create_database_engine(
        args.database_url,
        application_name="axiom-database-fingerprint",
        pool_size=1,
        max_overflow=0,
    )
    try:
        with engine.connect() as conn:
            revision = conn.execute(
                text(f'SELECT version_num FROM "{AXIOM_SCHEMA}".alembic_version')
            ).scalar_one()
            tables = []
            for table in metadata.sorted_tables:
                rows = [dict(row) for row in conn.execute(select(table)).mappings()]
                tables.append(
                    {"table": table.name, "count": len(rows), "sha256": _rows_hash(table, rows)}
                )
        payload = {"revision": revision, "tables": tables}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
