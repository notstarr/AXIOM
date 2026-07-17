from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from sqlalchemy import Engine

from axiom.api.v1.router import router as v1_router
from axiom.infrastructure.database.engine import create_sqlite_engine


def create_v2_app(
    *,
    engine: Engine | None = None,
    database_path: str | Path | None = None,
    data_dir: str | Path | None = None,
    service_token: str | None = None,
) -> FastAPI:
    app = FastAPI(
        title="AXIOM API V2",
        description="Modular Task/Fact/Intent/Todo backend",
        version="2.0.0",
    )
    app.state.v2_engine = engine or create_sqlite_engine(
        database_path or os.getenv("AXIOM_V2_DB") or "axiom-v2.db"
    )
    app.state.data_dir = Path(
        data_dir or os.getenv("AXIOM_DATA_DIR") or "~/.local/share/axiom"
    ).expanduser().resolve()
    app.state.service_token = service_token or os.getenv("AXIOM_SERVICE_TOKEN", "")
    app.include_router(v1_router)

    @app.get("/health", include_in_schema=True)
    def health() -> dict[str, str]:
        return {"status": "ok", "api": "v2"}

    return app


app = create_v2_app()
