from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from axiom.server import db
from axiom.server.app import app


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "axiom.db")
    with TestClient(app) as test_client:
        yield test_client


def _create_project(client: TestClient) -> str:
    response = client.post(
        "/projects",
        json={
            "title": "test",
            "origin": "starting point",
            "goal": "finish",
            "hints": [{"content": "initial clue", "creator": "human"}],
        },
    )
    assert response.status_code == 201
    assert response.json()["project"]["bootstrap_enabled"] is True
    return response.json()["project"]["id"]


def test_project_workflow_create_conclude_complete_and_reopen(client: TestClient) -> None:
    project_id = _create_project(client)

    response = client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "investigate", "creator": "reasoner", "worker": None},
    )
    assert response.status_code == 201
    assert response.json()["id"] == "i001"

    response = client.post(
        f"/projects/{project_id}/intents/i001/heartbeat",
        json={"worker": "explorer"},
    )
    assert response.status_code == 200
    assert response.json()["worker"] == "explorer"

    response = client.post(
        f"/projects/{project_id}/intents/i001/conclude",
        json={"worker": "explorer", "description": "new fact"},
    )
    assert response.status_code == 200
    assert response.json()["fact"] == {"id": "f001", "description": "new fact"}

    response = client.post(
        f"/projects/{project_id}/complete",
        json={"from": ["f001"], "description": "solved", "worker": "reasoner"},
    )
    assert response.status_code == 200
    assert response.json()["to"] == "goal"

    response = client.post(
        f"/projects/{project_id}/reopen",
        json={"description": "human correction", "creator": "human"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["project"]["status"] == "active"
    assert payload["fact"] == {"id": "f002", "description": "human correction"}
    assert payload["intent"]["from"] == ["f001"]
    assert payload["intent"]["to"] == "f002"


def test_stopping_project_releases_claims_and_reason_but_keeps_hints_writable(client: TestClient) -> None:
    project_id = _create_project(client)
    client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "work", "creator": "worker-a", "worker": "worker-a"},
    )
    client.post(
        f"/projects/{project_id}/reason/claim",
        json={"worker": "worker-b", "trigger": "facts:2->3"},
    )

    response = client.put(f"/projects/{project_id}/status", json={"status": "stopped"})
    assert response.status_code == 200
    assert response.json()["reason"] is None

    detail = client.get(f"/projects/{project_id}").json()
    assert detail["intents"][0]["worker"] is None
    assert client.post(
        f"/projects/{project_id}/hints",
        json={"content": "manual note", "creator": "human"},
    ).status_code == 201
    assert client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "blocked", "creator": "reasoner", "worker": None},
    ).status_code == 403


def test_intent_creation_rejects_goal_source_and_mismatched_initial_worker(client: TestClient) -> None:
    project_id = _create_project(client)

    assert client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["goal"], "description": "invalid", "creator": "reasoner", "worker": None},
    ).status_code == 400
    assert client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "invalid", "creator": "reasoner", "worker": "explorer"},
    ).status_code == 400


def test_settings_and_export_are_backed_by_the_same_database(client: TestClient) -> None:
    project_id = _create_project(client)

    response = client.put(
        "/settings",
        json={
            "intent_timeout": 30,
            "reason_timeout": 45,
            "bootstrap_worker_count": 1,
            "explore_worker_count": 2,
            "reason_worker_count": 1,
            "plan_worker_count": 1,
            "plan_timeout": 600,
            "proxy_url": "",
            "proxy_enabled": False,
            "domain_blacklist": [],
        },
    )
    assert response.status_code == 200
    assert client.get("/settings").json() == {
        "intent_timeout": 30,
        "reason_timeout": 45,
        "bootstrap_worker_count": 1,
        "explore_worker_count": 2,
        "reason_worker_count": 1,
        "plan_worker_count": 1,
        "plan_timeout": 600,
        "crawler_active_window_seconds": 180,
        "crawler_flush_window_seconds": 45,
        "crawler_total_timeout_seconds": 1800,
        "openai_base_url": "",
        "openai_model": "",
        "proxy_url": "",
        "proxy_enabled": False,
        "domain_blacklist": [],
    }

    exported = client.get(f"/projects/{project_id}/export?format=yaml")
    assert exported.status_code == 200
    assert "origin: starting point" in exported.text
    assert "goal: finish" in exported.text
    assert client.get(f"/projects/{project_id}/export?format=invalid").status_code == 400


def test_blackboard_item_crud_and_project_detail_integration(client: TestClient) -> None:
    project_id = _create_project(client)
    detail = client.get(f"/projects/{project_id}")
    group_id = detail.json()["project"]["parent_project_id"]

    # Use group-level blackboard API
    created = client.post(
        f"/project-groups/{group_id}/blackboard",
        json={
            "title": "Auth surface mapping",
            "content": "Track login, JWT, and role endpoints",
            "category": "scope",
            "status": "open",
            "priority": "high",
            "related_fact_id": "f001",
            "related_intent_id": "i001",
            "related_vuln_id": "v001",
            "creator": "human",
        },
    )
    assert created.status_code == 201
    item = created.json()
    assert item["id"] == "b001"
    assert item["updated_by"] == "human"
    assert item["priority"] == "high"
    assert item["related_vuln_id"] == "v001"

    listed = client.get(f"/project-groups/{group_id}/blackboard")
    assert listed.status_code == 200
    assert len(listed.json()) == 1

    updated = client.put(
        f"/project-groups/{group_id}/blackboard/{item['id']}",
        json={
            "title": "Auth surface mapped",
            "content": "JWT and role endpoints verified",
            "category": "analysis",
            "status": "in_progress",
            "priority": "medium",
            "related_fact_id": "f002",
            "related_intent_id": None,
            "related_vuln_id": "v001",
            "updated_by": "pi-worker",
        },
    )
    assert updated.status_code == 200
    assert updated.json()["status"] == "in_progress"
    assert updated.json()["updated_by"] == "pi-worker"
    assert updated.json()["related_fact_id"] == "f002"

    # Project detail should include group blackboard
    detail2 = client.get(f"/projects/{project_id}")
    assert detail2.status_code == 200
    assert len(detail2.json()["blackboard"]) == 1
    assert detail2.json()["blackboard"][0]["title"] == "Auth surface mapped"

    created2 = client.post(
        f"/project-groups/{group_id}/blackboard",
        json={
            "title": "Session checks",
            "content": "Track session fixation tests",
            "category": "test-plan",
            "status": "open",
            "priority": "low",
            "creator": "human",
        },
    )
    assert created2.status_code == 201

    deleted = client.delete(f"/project-groups/{group_id}/blackboard/{item['id']}")
    assert deleted.status_code == 204
    remaining = client.get(f"/project-groups/{group_id}/blackboard").json()
    assert len(remaining) == 1
    assert remaining[0]["id"] == "b002"


def test_settings_domain_blacklist_roundtrip(client: TestClient) -> None:
    response = client.put(
        "/settings",
        json={
            "intent_timeout": 15,
            "reason_timeout": 15,
            "proxy_url": "",
            "proxy_enabled": False,
            "domain_blacklist": ["https://git.kkroup.cn/path", "api.example.com", "api.example.com"],
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["domain_blacklist"] == ["git.kkroup.cn", "api.example.com"]

    fetched = client.get("/settings")
    assert fetched.status_code == 200
    assert fetched.json()["domain_blacklist"] == ["git.kkroup.cn", "api.example.com"]


def test_expired_intent_and_reason_leases_can_be_reclaimed(client: TestClient) -> None:
    project_id = _create_project(client)
    client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "work", "creator": "worker-a", "worker": "worker-a"},
    )
    client.post(
        f"/projects/{project_id}/reason/claim",
        json={"worker": "worker-a", "trigger": "bootstrap"},
    )
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE intents SET last_heartbeat_at = '2000-01-01T00:00:00Z' WHERE project_id = ?",
            (project_id,),
        )
        conn.execute(
            "UPDATE projects SET reason_last_heartbeat_at = '2000-01-01T00:00:00Z' WHERE id = ?",
            (project_id,),
        )

    response = client.post(
        f"/projects/{project_id}/intents/i001/heartbeat",
        json={"worker": "worker-b"},
    )
    assert response.status_code == 200
    assert response.json()["worker"] == "worker-b"

    response = client.post(
        f"/projects/{project_id}/reason/claim",
        json={"worker": "worker-b", "trigger": "facts:2->3"},
    )
    assert response.status_code == 200
    assert response.json()["reason"]["worker"] == "worker-b"


def test_live_reason_lease_rejects_competing_worker(client: TestClient) -> None:
    project_id = _create_project(client)
    assert client.post(
        f"/projects/{project_id}/reason/claim",
        json={"worker": "worker-a", "trigger": "bootstrap"},
    ).status_code == 200

    response = client.post(
        f"/projects/{project_id}/reason/claim",
        json={"worker": "worker-b", "trigger": "facts:2->3"},
    )

    assert response.status_code == 409
    assert "worker-a" in response.json()["detail"]


def test_project_creation_persists_disabled_bootstrap_and_exports_it(client: TestClient) -> None:
    response = client.post(
        "/projects",
        json={
            "title": "no bootstrap",
            "origin": "start",
            "goal": "finish",
            "bootstrap_enabled": False,
        },
    )

    assert response.status_code == 201
    project_id = response.json()["project"]["id"]
    assert client.get(f"/projects/{project_id}").json()["project"]["bootstrap_enabled"] is False
    assert "bootstrap_enabled: false" in client.get(f"/projects/{project_id}/export?format=yaml").text


def test_project_creation_rejects_invalid_bootstrap_enabled(client: TestClient) -> None:
    response = client.post(
        "/projects",
        json={
            "title": "invalid bootstrap",
            "origin": "start",
            "goal": "finish",
            "bootstrap_enabled": "sometimes",
        },
    )

    assert response.status_code == 422


def test_dispatch_settings_roundtrip_uses_database_and_exposes_runtime_state(client: TestClient) -> None:
    response = client.put(
        "/settings/dispatch",
        json={
            "runtime_interval": 7,
            "runtime_max_workers": 9,
            "runtime_max_running_projects": 4,
            "runtime_max_project_workers": 3,
            "runtime_healthcheck_timeout": 25,
            "runtime_worker_healthcheck": "startup_only",
            "runtime_prompt_group": "default",
            "bootstrap_timeout": 320,
            "bootstrap_conclude_timeout": 95,
            "explore_timeout": 33,
            "explore_conclude_timeout": 91,
            "reason_timeout": 47,
            "reason_max_intents": 4,
            "workers": [
                {
                    "name": "alpha",
                    "task_types": ["bootstrap", "reason"],
                    "max_running": 2,
                    "priority": 1,
                    "pi_model": "gpt-test",
                    "pi_base_url": "https://api.example.com/v1",
                    "pi_provider_api": "openai-completions",
                    "pi_model_context_window": "128000",
                    "pi_api_key": "",
                    "pi_api_key_masked": "",
                }
            ],
            "proxy_url": "socks5://127.0.0.1:1080",
            "proxy_username": "",
            "proxy_password": "",
            "proxy_enabled": True,
            "bootstrap_worker_count": 1,
            "explore_worker_count": 2,
            "reason_worker_count": 1,
        },
    )
    assert response.status_code == 200

    payload = response.json()
    assert payload["runtime_interval"] == 7
    assert payload["runtime_max_workers"] == 9
    assert payload["bootstrap_timeout"] == 320
    assert payload["explore_timeout"] == 33
    assert payload["reason_timeout"] == 47
    assert payload["config_version"] >= 1
    assert payload["workers"][0]["name"] == "alpha"
    assert payload["bootstrap_worker_count"] == 1
    assert payload["explore_worker_count"] == 2
    assert payload["reason_worker_count"] == 1

    settings_payload = client.get("/settings").json()
    assert settings_payload["intent_timeout"] == 33
    assert settings_payload["reason_timeout"] == 47
    assert settings_payload["proxy_enabled"] is True
