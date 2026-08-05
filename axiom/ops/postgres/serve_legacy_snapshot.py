#!/usr/bin/env python3
"""Serve a frozen SQLite copy through the pre-PostgreSQL AXIOM API without scans."""
from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from axiom.app import create_app
from axiom.modules.axiom_tools.service import AxiomToolService
from axiom.modules.phase_capabilities.service import PhaseCapabilityService
from axiom.shared.database import create_sqlite_engine


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sqlite-path", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()

    # The normal legacy app scan updates discovered_at/updated_at.  A migration
    # snapshot must be a pure read of the immutable source copy.
    AxiomToolService.scan = lambda self: None  # type: ignore[method-assign]
    PhaseCapabilityService._initialize_existing_skills_once = (  # type: ignore[method-assign]
        lambda self: None
    )
    engine = create_sqlite_engine(args.sqlite_path)
    uvicorn.run(
        create_app(engine=engine, data_dir=args.data_dir.resolve()),
        host=args.host,
        port=args.port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
