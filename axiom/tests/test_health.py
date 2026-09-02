from fastapi.testclient import TestClient
from sqlalchemy import func, select

from axiom.app import create_app
from axiom.modules.axiom_tools.tables import axiom_tools
from axiom.modules.phase_capabilities.tables import phase_capability_bindings
from axiom.shared.maintenance import write_maintenance


def test_health(client) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["api"] == "v1"
    assert payload["database"]["backend"] == "postgresql"
    assert payload["database"]["migration"] == "20260902_pg0013"
    assert payload["database"]["pool"]["size"] >= 1
    assert payload["maintenance"]["state"] == "normal"


def test_drain_blocks_new_work_but_keeps_reads_available(client, data_dir) -> None:
    write_maintenance(data_dir, state="drain", actor="pytest", reason="cutover")

    blocked = client.post(
        "/api/v1/tasks",
        json={
            "project_id": "prj_missing",
            "title": "blocked",
            "origin": "blocked.example.com",
            "goal": "blocked",
        },
    )
    assert blocked.status_code == 503
    assert blocked.json()["error"]["code"] == "maintenance_mode"
    assert client.get("/health").status_code == 200
    assert client.get("/api/v1/projects").status_code == 200


def test_frozen_blocks_all_http_writes_and_exposes_state(client, data_dir) -> None:
    write_maintenance(data_dir, state="frozen", actor="pytest", reason="final import")

    blocked = client.post("/api/v1/projects", json={"title": "blocked"})
    assert blocked.status_code == 503
    assert blocked.headers["Retry-After"] == "5"
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["maintenance"]["state"] == "frozen"
    assert health.headers["X-AXIOM-Maintenance"] == "frozen"


def test_frozen_startup_does_not_seed_database_or_marker(engine, data_dir) -> None:
    write_maintenance(data_dir, state="frozen", actor="pytest", reason="immutable startup")

    with TestClient(create_app(engine=engine, data_dir=data_dir)) as frozen_client:
        assert frozen_client.get("/health").status_code == 200

    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(axiom_tools)).scalar_one() == 0
        assert (
            conn.execute(select(func.count()).select_from(phase_capability_bindings)).scalar_one()
            == 0
        )
    assert not (data_dir / ".phase-capabilities-v1-initialized").exists()
