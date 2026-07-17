from __future__ import annotations

from fastapi.testclient import TestClient

from axiom.infrastructure.database.engine import create_sqlite_engine
from axiom.infrastructure.database.metadata import metadata
from axiom.server.v2_app import create_v2_app


def test_v2_app_exposes_only_versioned_business_api(tmp_path) -> None:
    engine = create_sqlite_engine(tmp_path / "axiom-v2.db")
    metadata.create_all(engine)
    app = create_v2_app(
        engine=engine,
        data_dir=tmp_path / "data",
        service_token="service-token",
    )

    with TestClient(app) as client:
        assert "AXIOM" in client.get("/").text
        assert client.get("/static/v2/app.js").status_code == 200
        assert client.get("/health").json()["status"] == "ok"
        assert client.get("/api/v1/projects").status_code == 200
        assert client.get("/projects").status_code == 404
        paths = client.get("/openapi.json").json()["paths"]
        assert "/api/v1/tasks" in paths
        assert all(path == "/health" or path.startswith("/api/v1/") for path in paths)
