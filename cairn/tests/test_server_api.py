from __future__ import annotations

import json

from fastapi.testclient import TestClient
import pytest
import yaml

from axiom.server import db
from axiom.server.app import app
from axiom.server.routers import settings as settings_router


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


def test_delete_fact_reopens_producing_intent_for_rerun(client: TestClient) -> None:
    project_id = _create_project(client)
    intent_id = "i001"
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO intents (id, project_id, to_fact_id, description, creator, worker, last_heartbeat_at, created_at, concluded_at) "
            "VALUES (?, ?, NULL, ?, ?, NULL, NULL, ?, NULL)",
            (intent_id, project_id, "investigate", "reasoner", "2026-01-01T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO intent_sources (intent_id, project_id, fact_id) VALUES (?, ?, ?)",
            (intent_id, project_id, "origin"),
        )
    assert client.post(
        f"/projects/{project_id}/intents/{intent_id}/heartbeat",
        json={"worker": "explorer"},
    ).status_code == 200
    concluded = client.post(
        f"/projects/{project_id}/intents/{intent_id}/conclude",
        json={"worker": "explorer", "description": "new fact"},
    )
    assert concluded.status_code == 200
    produced_fact_id = concluded.json()["fact"]["id"]

    deleted = client.delete(f"/projects/{project_id}/facts/{produced_fact_id}")
    assert deleted.status_code == 204

    detail = client.get(f"/projects/{project_id}").json()
    intent = next(i for i in detail["intents"] if i["id"] == intent_id)
    assert intent["to"] is None
    assert intent["concluded_at"] is None
    assert intent["worker"] is None
    assert intent["last_heartbeat_at"] is None

    reclaimed = client.post(
        f"/projects/{project_id}/intents/{intent_id}/heartbeat",
        json={"worker": "re-runner"},
    )
    assert reclaimed.status_code == 200
    assert reclaimed.json()["worker"] == "re-runner"


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


def test_crawler_state_views_can_be_read_without_physical_yaml_files(client: TestClient) -> None:
    project_id = _create_project(client)
    detail = client.get(f"/projects/{project_id}").json()
    group_id = detail["project"]["parent_project_id"]

    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO crawler_pages (group_id, page_id, data_json, updated_at) VALUES (?, ?, ?, ?)",
            (
                group_id,
                "page_home",
                json.dumps(
                    {
                        "id": "page_home",
                        "name": "Home",
                        "url": "https://example.com/home",
                        "path": "/home",
                        "page_type": "landing",
                    },
                    ensure_ascii=False,
                ),
                "2026-06-18T00:00:00Z",
            ),
        )

    listing = client.get(f"/file-groups/{group_id}/crawler")
    assert listing.status_code == 200
    names = {item["name"] for item in listing.json()["files"]}
    assert "site_map.yaml" in names

    site_map = client.get(f"/file-groups/{group_id}/crawler/site_map.yaml")
    assert site_map.status_code == 200
    assert "page_home" in site_map.json()["content"]


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
    intent_id = "i001"
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO intents (id, project_id, to_fact_id, description, creator, worker, last_heartbeat_at, created_at, concluded_at) "
            "VALUES (?, ?, NULL, ?, ?, ?, ?, ?, NULL)",
            (intent_id, project_id, "work", "worker-a", "worker-a", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO intent_sources (intent_id, project_id, fact_id) VALUES (?, ?, ?)",
            (intent_id, project_id, "origin"),
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
        f"/projects/{project_id}/intents/{intent_id}/heartbeat",
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


def test_heartbeat_rejects_concluded_intent_even_if_to_fact_is_missing(client: TestClient) -> None:
    project_id = _create_project(client)
    intent_id = "i001"
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO intents (id, project_id, to_fact_id, description, creator, worker, last_heartbeat_at, created_at, concluded_at) "
            "VALUES (?, ?, NULL, ?, ?, NULL, NULL, ?, NULL)",
            (intent_id, project_id, "work", "worker-a", "2026-01-01T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO intent_sources (intent_id, project_id, fact_id) VALUES (?, ?, ?)",
            (intent_id, project_id, "origin"),
        )
    assert client.post(
        f"/projects/{project_id}/intents/{intent_id}/heartbeat",
        json={"worker": "worker-a"},
    ).status_code == 200
    assert client.post(
        f"/projects/{project_id}/intents/{intent_id}/conclude",
        json={"worker": "worker-a", "description": "done fact"},
    ).status_code == 200
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE intents SET to_fact_id = NULL WHERE project_id = ? AND id = ?",
            (project_id, intent_id),
        )

    response = client.post(
        f"/projects/{project_id}/intents/{intent_id}/heartbeat",
        json={"worker": "worker-b"},
    )
    assert response.status_code == 409
    assert "already concluded" in response.json()["detail"]


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


def test_dispatch_models_endpoint_uses_worker_env_and_parses_models(client: TestClient, monkeypatch) -> None:
    worker_name = "alpha"
    client.put(
        "/settings/dispatch",
        json={
            "runtime_interval": 3,
            "runtime_max_workers": 8,
            "runtime_max_running_projects": 3,
            "runtime_max_project_workers": 4,
            "runtime_healthcheck_timeout": 20,
            "runtime_worker_healthcheck": "startup_only",
            "runtime_prompt_group": "default",
            "bootstrap_timeout": 300,
            "bootstrap_conclude_timeout": 90,
            "explore_timeout": 300,
            "explore_conclude_timeout": 90,
            "reason_timeout": 300,
            "reason_max_intents": 2,
            "depth_reason_max_intents": 5,
            "workers": [
                {
                    "name": worker_name,
                    "task_types": ["bootstrap", "reason"],
                    "max_running": 1,
                    "priority": 0,
                    "pi_model": "demo",
                    "pi_base_url": "https://api.example.com/v1",
                    "pi_provider_api": "openai-completions",
                    "pi_model_context_window": "128000",
                    "pi_api_key": "secret",
                    "pi_api_key_masked": "",
                    "disabled": False,
                }
            ],
            "proxy_url": "",
            "proxy_username": "",
            "proxy_password": "",
            "proxy_enabled": False,
            "bootstrap_worker_count": 1,
            "explore_worker_count": 2,
            "reason_worker_count": 1,
            "plan_timeout": 600,
            "plan_worker_count": 1,
        },
    )

    captured: dict[str, object] = {}

    class _Resp:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {"data": [{"id": "gpt-4o", "context_window": 200000}, {"id": "gpt-5"}]}

    def _fake_get(url, headers=None, timeout=0):
        captured["url"] = url
        captured["headers"] = headers or {}
        captured["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(settings_router.requests, "get", _fake_get)
    response = client.get(f"/settings/dispatch/models?worker_name={worker_name}")
    assert response.status_code == 200
    payload = response.json()
    assert payload["error"] == ""
    assert payload["models"] == [
        {"id": "gpt-4o", "context_window": 200000},
        {"id": "gpt-5", "context_window": 1000000},
    ]
    assert captured["url"] == "https://api.example.com/v1/models"
    assert captured["headers"] == {"Authorization": "Bearer secret"}


def test_test_pi_connection_falls_back_to_dispatch_worker_key(client: TestClient, monkeypatch) -> None:
    worker_name = "alpha"
    client.put(
        "/settings/dispatch",
        json={
            "runtime_interval": 3,
            "runtime_max_workers": 8,
            "runtime_max_running_projects": 3,
            "runtime_max_project_workers": 4,
            "runtime_healthcheck_timeout": 20,
            "runtime_worker_healthcheck": "startup_only",
            "runtime_prompt_group": "default",
            "bootstrap_timeout": 300,
            "bootstrap_conclude_timeout": 90,
            "explore_timeout": 300,
            "explore_conclude_timeout": 90,
            "reason_timeout": 300,
            "reason_max_intents": 2,
            "depth_reason_max_intents": 5,
            "workers": [
                {
                    "name": worker_name,
                    "task_types": ["bootstrap", "reason"],
                    "max_running": 1,
                    "priority": 0,
                    "pi_model": "demo",
                    "pi_base_url": "https://api.example.com/v1",
                    "pi_provider_api": "openai-completions",
                    "pi_model_context_window": "128000",
                    "pi_api_key": "secret-in-db",
                    "pi_api_key_masked": "",
                    "disabled": False,
                }
            ],
            "proxy_url": "",
            "proxy_username": "",
            "proxy_password": "",
            "proxy_enabled": False,
            "bootstrap_worker_count": 1,
            "explore_worker_count": 2,
            "reason_worker_count": 1,
            "plan_timeout": 600,
            "plan_worker_count": 1,
        },
    )

    captured: dict[str, object] = {}

    class _Resp:
        status_code = 200
        text = ""
        reason = ""

    def _fake_post(url, headers=None, json=None, timeout=0):
        captured["url"] = url
        captured["headers"] = headers or {}
        captured["json"] = json or {}
        captured["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(settings_router.requests, "post", _fake_post)
    response = client.post(
        "/settings/test-pi",
        json={
            "worker_name": worker_name,
            "pi_base_url": "https://api.example.com/v1",
            "pi_model": "demo",
            "pi_provider_api": "openai-completions",
            "pi_api_key": "",
        },
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert captured["headers"] == {
        "Authorization": "Bearer secret-in-db",
        "Content-Type": "application/json",
    }


def test_update_dispatch_settings_uses_keyring_fallback_for_empty_worker_key(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(settings_router, "load_provider_api_key", lambda provider: "fallback-key" if provider == "pi" else "")
    response = client.put(
        "/settings/dispatch",
        json={
            "runtime_interval": 3,
            "runtime_max_workers": 8,
            "runtime_max_running_projects": 3,
            "runtime_max_project_workers": 4,
            "runtime_healthcheck_timeout": 20,
            "runtime_worker_healthcheck": "startup_only",
            "runtime_prompt_group": "default",
            "bootstrap_timeout": 300,
            "bootstrap_conclude_timeout": 90,
            "explore_timeout": 300,
            "explore_conclude_timeout": 90,
            "reason_timeout": 300,
            "reason_max_intents": 2,
            "depth_reason_max_intents": 5,
            "workers": [
                {
                    "name": "no-key-worker",
                    "task_types": ["bootstrap"],
                    "max_running": 1,
                    "priority": 0,
                    "pi_model": "demo",
                    "pi_base_url": "https://api.example.com/v1",
                    "pi_provider_api": "openai-completions",
                    "pi_model_context_window": "128000",
                    "pi_api_key": "",
                    "pi_api_key_masked": "",
                    "disabled": False,
                }
            ],
            "proxy_url": "",
            "proxy_username": "",
            "proxy_password": "",
            "proxy_enabled": False,
            "bootstrap_worker_count": 1,
            "explore_worker_count": 2,
            "reason_worker_count": 1,
            "plan_timeout": 600,
            "plan_worker_count": 1,
        },
    )
    assert response.status_code == 200
    with db.get_conn() as conn:
        row = conn.execute("SELECT env_json FROM dispatch_workers WHERE name = ?", ("no-key-worker",)).fetchone()
        assert row is not None
        env = json.loads(row["env_json"])
        assert env["PI_API_KEY"] == "fallback-key"
    fetched = client.get("/settings/dispatch").json()
    worker = next(item for item in fetched["workers"] if item["name"] == "no-key-worker")
    assert worker["pi_api_key_masked"] == "fall****-key"


def test_intent_scope_filtering(client: TestClient) -> None:
    project_id = _create_project(client)
    detail = client.get(f"/projects/{project_id}").json()
    group_id = detail["project"]["parent_project_id"]

    # 1. 仅白名单模式
    response = client.put(
        f"/project-groups/{group_id}",
        json={
            "title": "test group",
            "description": "test description",
            "scope": {
                "targets": ["*.b.com", "10.0.0.1"],
                "exclude": [],
                "notes": "test notes"
            },
            "status": "active"
        }
    )
    assert response.status_code == 200

    # 2. 命中白名单 -> 放行
    response = client.post(
        f"/projects/{project_id}/intents",
        json={
            "from": ["origin"],
            "description": "Explore api.b.com for SQL injection",
            "targets": ["api.b.com"],
            "creator": "reasoner",
            "worker": None
        },
    )
    assert response.status_code == 201
    assert response.json()["id"] == "i001"

    # 3. 未命中白名单 -> 拒绝
    response = client.post(
        f"/projects/{project_id}/intents",
        json={
            "from": ["origin"],
            "description": "Explore api.a.com for SQL injection",
            "targets": ["api.a.com"],
            "creator": "reasoner",
            "worker": None
        },
    )
    assert response.status_code == 400
    assert "do not match whitelist" in response.json()["detail"]

    # 3.1 强制期：缺失 targets 直接拒绝
    response = client.post(
        f"/projects/{project_id}/intents",
        json={
            "from": ["origin"],
            "description": "Explore api.b.com for SQL injection",
            "creator": "reasoner",
            "worker": None
        },
    )
    assert response.status_code == 400
    assert "intent.targets is required" in response.json()["detail"]

    # 4. 切换为仅黑名单模式
    response = client.put(
        f"/project-groups/{group_id}",
        json={
            "title": "test group",
            "description": "test description",
            "scope": {
                "targets": [],
                "exclude": ["forbidden.b.com"],
                "notes": "blacklist mode"
            },
            "status": "active"
        }
    )
    assert response.status_code == 200

    # 5. 命中黑名单 -> 拒绝
    response = client.post(
        f"/projects/{project_id}/intents",
        json={
            "from": ["origin"],
            "description": "Explore forbidden.b.com for SQL injection",
            "targets": ["forbidden.b.com"],
            "creator": "reasoner",
            "worker": None
        },
    )
    assert response.status_code == 400
    assert "matched blacklist rule" in response.json()["detail"]

    # 6. 黑名单未命中 -> 放行
    response = client.post(
        f"/projects/{project_id}/intents",
        json={
            "from": ["origin"],
            "description": "Explore normal.b.com for SQL injection",
            "targets": ["normal.b.com"],
            "creator": "reasoner",
            "worker": None
        },
    )
    assert response.status_code == 201


def test_intent_timeouts_roundtrip(client: TestClient) -> None:
    project_id = _create_project(client)
    response = client.post(
        f"/projects/{project_id}/intents",
        json={
            "from": ["origin"],
            "description": "Explore api.b.com timeout budget",
            "targets": ["api.b.com"],
            "timeouts": {"execute_seconds": 420, "conclude_seconds": 120},
            "creator": "reasoner",
            "worker": None,
        },
    )
    assert response.status_code == 201
    assert response.json()["timeouts"] == {"execute_seconds": 420, "conclude_seconds": 120}

    detail = client.get(f"/projects/{project_id}")
    assert detail.status_code == 200
    assert detail.json()["intents"][0]["timeouts"] == {"execute_seconds": 420, "conclude_seconds": 120}


def test_export_yaml_keeps_bootstrap_fact_full_but_truncates_regular_fact(client: TestClient) -> None:
    project_id = _create_project(client)

    bootstrap_create = client.post(
        f"/projects/{project_id}/intents",
        json={
            "from": ["origin"],
            "description": "bootstrap",
            "targets": ["teamshare-web.saas-test.cn-beijing.volce.teamshare.work"],
            "creator": "dispatcher.bootstrap",
            "worker": None,
        },
    )
    assert bootstrap_create.status_code == 201
    bootstrap_id = bootstrap_create.json()["id"]
    assert client.post(
        f"/projects/{project_id}/intents/{bootstrap_id}/heartbeat",
        json={"worker": "bootstrap-worker"},
    ).status_code == 200
    bootstrap_fact = (
        "BOOTSTRAP_FACT_BEGIN "
        + ("A" * 120)
        + " BOOTSTRAP_FACT_END"
    )
    assert client.post(
        f"/projects/{project_id}/intents/{bootstrap_id}/conclude",
        json={"worker": "bootstrap-worker", "description": bootstrap_fact},
    ).status_code == 200

    regular_create = client.post(
        f"/projects/{project_id}/intents",
        json={
            "from": ["f001"],
            "description": "regular explore",
            "targets": ["api.b.com"],
            "creator": "reasoner",
            "worker": None,
        },
    )
    assert regular_create.status_code == 201
    regular_id = regular_create.json()["id"]
    assert client.post(
        f"/projects/{project_id}/intents/{regular_id}/heartbeat",
        json={"worker": "explorer"},
    ).status_code == 200
    regular_fact = "REGULAR_FACT_BEGIN " + ("B" * 140) + " REGULAR_FACT_END"
    assert client.post(
        f"/projects/{project_id}/intents/{regular_id}/conclude",
        json={"worker": "explorer", "description": regular_fact},
    ).status_code == 200

    exported = client.get(f"/projects/{project_id}/export?format=yaml")
    assert exported.status_code == 200
    assert "BOOTSTRAP_FACT_END" in exported.text
    assert "REGULAR_FACT_END" not in exported.text


def test_export_intents_embed_lookup_metadata(client: TestClient) -> None:
    project_id = _create_project(client)
    long_description = "跨组织写操作验证" + ("-细节" * 40)
    created = client.post(
        f"/projects/{project_id}/intents",
        json={
            "from": ["origin"],
            "description": long_description,
            "targets": ["api.example.com"],
            "creator": "reasoner",
            "worker": None,
        },
    )
    assert created.status_code == 201
    intent_id = created.json()["id"]

    exported = client.get(f"/projects/{project_id}/export?format=yaml")
    assert exported.status_code == 200
    payload = yaml.safe_load(exported.text)

    assert payload["project"]["id"] == project_id
    assert "intent_index" not in payload
    intent_row = next(row for row in payload["intents"] if row["id"] == intent_id)
    assert intent_row["lookup_path"] == f"/projects/{project_id}?intent_id={intent_id}"
    assert intent_row["status"] == "open"
    assert intent_row["description"].endswith("...")
    assert "creator" not in intent_row
    assert "worker" not in intent_row
    assert "created_at" not in intent_row
    assert "concluded_at" not in intent_row
    fact_row = next(row for row in payload["facts"] if row["id"] == "origin")
    assert fact_row["lookup_path"] == f"/projects/{project_id}?fact_id=origin"


def test_project_detail_supports_intent_id_filter(client: TestClient) -> None:
    project_id = _create_project(client)
    long_description = "intent-description-" + ("x" * 120)
    first = client.post(
        f"/projects/{project_id}/intents",
        json={
            "from": ["origin"],
            "description": long_description,
            "targets": ["api.example.com"],
            "creator": "reasoner",
            "worker": None,
        },
    )
    assert first.status_code == 201
    second = client.post(
        f"/projects/{project_id}/intents",
        json={
            "from": ["origin"],
            "description": "second-intent-" + ("y" * 120),
            "targets": ["api2.example.com"],
            "creator": "reasoner",
            "worker": None,
        },
    )
    assert second.status_code == 201
    target_id = second.json()["id"]

    filtered = client.get(f"/projects/{project_id}?intent_id={target_id}")
    assert filtered.status_code == 200
    body = filtered.json()
    assert len(body["intents"]) == 1
    assert len(body["facts"]) == 0
    assert len(body["hints"]) == 0
    assert len(body["vulns"]) == 0
    assert len(body["blackboard"]) == 0
    assert body["intents"][0]["id"] == target_id
    assert body["intents"][0]["lookup_path"] == f"/projects/{project_id}?intent_id={target_id}"
    assert body["intents"][0]["status"] == "open"
    assert body["intents"][0]["description"] == "second-intent-" + ("y" * 120)

    missing = client.get(f"/projects/{project_id}?intent_id=i999")
    assert missing.status_code == 404


def test_project_detail_supports_fact_id_filter(client: TestClient) -> None:
    project_id = _create_project(client)
    intent = client.post(
        f"/projects/{project_id}/intents",
        json={
            "from": ["origin"],
            "description": "collect fact",
            "targets": ["api.example.com"],
            "creator": "reasoner",
            "worker": None,
        },
    )
    assert intent.status_code == 201
    iid = intent.json()["id"]
    assert client.post(
        f"/projects/{project_id}/intents/{iid}/heartbeat",
        json={"worker": "explorer"},
    ).status_code == 200
    assert client.post(
        f"/projects/{project_id}/intents/{iid}/conclude",
        json={"worker": "explorer", "description": "new fact body " + ("z" * 120)},
    ).status_code == 200

    filtered = client.get(f"/projects/{project_id}?fact_id=f001")
    assert filtered.status_code == 200
    body = filtered.json()
    assert len(body["intents"]) == 0
    assert len(body["facts"]) == 1
    assert len(body["hints"]) == 0
    assert len(body["vulns"]) == 0
    assert len(body["blackboard"]) == 0
    assert body["facts"][0]["id"] == "f001"
    assert body["facts"][0]["lookup_path"] == f"/projects/{project_id}?fact_id=f001"
    assert body["facts"][0]["status"] == "active"
    assert body["facts"][0]["description"] == "new fact body " + ("z" * 120)

    missing = client.get(f"/projects/{project_id}?fact_id=f999")
    assert missing.status_code == 404


def test_project_group_scope_rejects_dual_whitelist_blacklist(client: TestClient) -> None:
    project_id = _create_project(client)
    group_id = client.get(f"/projects/{project_id}").json()["project"]["parent_project_id"]
    response = client.put(
        f"/project-groups/{group_id}",
        json={
            "title": "test group",
            "description": "test description",
            "scope": {
                "targets": ["api.b.com"],
                "exclude": ["forbidden.b.com"],
                "notes": ""
            },
            "status": "active"
        },
    )
    assert response.status_code == 422


def test_assets_post_deduplicates_and_merges(client: TestClient) -> None:
    project_id = _create_project(client)
    first = client.post(
        "/assets",
        json={
            "project_id": project_id,
            "group_id": "pg_001",
            "backend_domain": "teamshare-service.example",
            "api_path": "/api/v2/assignments/queries",
            "api_method": "POST",
            "api_purpose": "",
            "auth_required": "unknown",
            "test_status": "untested",
        },
    )
    assert first.status_code == 200
    first_id = first.json()["id"]

    second = client.post(
        "/assets",
        json={
            "project_id": project_id,
            "group_id": "pg_001",
            "backend_domain": "teamshare-service.example",
            "api_path": "/api/v2/assignments/queries",
            "api_method": "POST",
            "api_purpose": "任务列表查询",
            "auth_required": "yes",
            "test_status": "vulnerable",
            "source_file": "/tmp/evidence.md",
        },
    )
    assert second.status_code == 200
    body = second.json()
    assert body["id"] == first_id
    assert body["api_purpose"] == "任务列表查询"
    assert body["auth_required"] == "yes"
    assert body["test_status"] == "vulnerable"
    assert body["source_file"] == "/tmp/evidence.md"


def test_credentials_post_upserts_same_label_with_normalization(client: TestClient) -> None:
    first = client.post(
        "/credentials",
        json={
            "group_id": "pg_001",
            "label": "user0319",
            "auth_type": "jwt",
            "credentials_json": "{\"authorization\":\"Bearer token-a\"}",
            "domains_json": "[\"api.example.com\"]",
            "source_task_id": "i001",
        },
    )
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["label"] == "pg_001.user0319.jwt.unknown"

    second = client.post(
        "/credentials",
        json={
            "group_id": "pg_001",
            "label": "pg_001.user0319.jwt.unknown",
            "auth_type": "jwt",
            "credentials_json": "{\"authorization\":\"Bearer token-b\"}",
            "domains_json": "[\"api.example.com\",\"teamshare.example\"]",
            "source_task_id": "i002",
        },
    )
    assert second.status_code == 200
    second_body = second.json()
    assert second_body["id"] == first_body["id"]
    assert second_body["credentials_json"] == "{\"authorization\":\"Bearer token-b\"}"
    assert second_body["source_task_id"] == "i002"


def test_credentials_reject_invalid_json_fields(client: TestClient) -> None:
    bad_credentials = client.post(
        "/credentials",
        json={
            "group_id": "pg_001",
            "label": "pg_001.user.jwt.test",
            "auth_type": "jwt",
            "credentials_json": "[]",
            "domains_json": "[\"api.example.com\"]",
        },
    )
    assert bad_credentials.status_code == 422

    bad_domains = client.post(
        "/credentials",
        json={
            "group_id": "pg_001",
            "label": "pg_001.user.jwt.test",
            "auth_type": "jwt",
            "credentials_json": "{\"authorization\":\"Bearer token\"}",
            "domains_json": "{\"domain\":\"api.example.com\"}",
        },
    )
    assert bad_domains.status_code == 422
