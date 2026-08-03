from __future__ import annotations

import json
from pathlib import Path
import shutil

import pytest

from axiom.modules.projects.tables import projects
from axiom.modules.reason.tables import reason_runs
from axiom.modules.tasks.tables import tasks
from axiom.shared.clock import utcnow


def _write_tool(root: Path, key: str, *, phases=None, schema=None) -> Path:
    package = root / key
    package.mkdir(parents=True, exist_ok=True)
    (package / "tool.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "key": key,
                "title": key,
                "version": "1.0.0",
                "description": "test tool",
                "entrypoint": "index.ts",
                "supported_phases": phases or ["bootstrap", "reason", "explore"],
                "risk_tags": ["test"],
                "parameters": schema
                if schema is not None
                else {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
                "prompt_guidelines": ["仅用于测试"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (package / "index.ts").write_text(
        "export default function testTool(pi) { void pi; }\n", encoding="utf-8"
    )
    return package


def test_bundled_axiom_tools_are_discovered_enabled_and_unbound(client) -> None:
    response = client.get("/api/v1/axiom-tools")

    assert response.status_code == 200, response.text
    items = response.json()
    assert {item["key"] for item in items} == {
        "search_text",
        "find_files",
        "list_directory",
        "http_request",
        "run_python",
    }
    assert all(item["status"] == "ready" for item in items)
    assert all(item["enabled"] is True for item in items)
    assert all(item["bindings"] == [] for item in items)


def test_scan_is_idempotent_and_preserves_missing_bindings(client, data_dir) -> None:
    root = data_dir / "axiom-tools"
    package = _write_tool(root, "managed_demo", phases=["explore"])

    first = client.post("/api/v1/axiom-tools/scan")
    second = client.post("/api/v1/axiom-tools/scan")
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    item = next(item for item in second.json()["items"] if item["key"] == "managed_demo")
    assert item["status"] == "ready"

    client.put(
        "/api/v1/workers/worker-a",
        json={"capabilities": ["explore"], "config": {"model": "m"}},
    )
    bound = client.put(
        "/api/v1/workers/worker-a/axiom-tools",
        json={"bootstrap": [], "reason": [], "explore": ["managed_demo"]},
    )
    assert bound.status_code == 200, bound.text
    shutil.rmtree(package)
    missing = client.post("/api/v1/axiom-tools/scan")
    assert missing.status_code == 200, missing.text
    item = client.get("/api/v1/axiom-tools/managed_demo").json()
    assert item["status"] == "missing"
    assert item["bindings"][0]["worker_name"] == "worker-a"


@pytest.mark.parametrize(
    "schema",
    [
        {},
        {"type": "string"},
        {"type": "object", "required": ["missing"], "properties": {}},
        {"type": "object", "properties": {"x": "bad"}},
    ],
)
def test_invalid_manifest_schema_is_not_ready(client, data_dir, schema) -> None:
    _write_tool(data_dir / "axiom-tools", "bad_schema", schema=schema)

    response = client.post("/api/v1/axiom-tools/scan")

    assert response.status_code == 200, response.text
    item = client.get("/api/v1/axiom-tools/bad_schema").json()
    assert item["status"] == "invalid"
    assert item["status_error"]


def test_reserved_duplicate_and_symlink_packages_are_rejected(client, data_dir) -> None:
    root = data_dir / "axiom-tools"
    _write_tool(root, "read")
    _write_tool(root, "search_text")
    symlink_target = root / "real_target"
    _write_tool(root, "real_target")
    symlink = root / "linked_tool"
    try:
        symlink.symlink_to(symlink_target, target_is_directory=True)
    except OSError:
        pytest.skip("filesystem does not support directory symlinks")

    response = client.post("/api/v1/axiom-tools/scan")

    assert response.status_code == 200, response.text
    assert client.get("/api/v1/axiom-tools/read").json()["status"] == "invalid"
    assert client.get("/api/v1/axiom-tools/search_text").json()["status"] == "invalid"
    linked = next(item for item in response.json()["items"] if item["key"] == "linked_tool")
    assert linked["status"] == "invalid"


def test_binding_is_validated_and_global_disable_keeps_effective_state(client) -> None:
    client.put(
        "/api/v1/workers/worker-a",
        json={"capabilities": ["explore"], "config": {"model": "m"}},
    )
    client.put(
        "/api/v1/workers/worker-b",
        json={"capabilities": ["bootstrap", "reason", "explore"], "config": {"model": "m"}, "disabled": True},
    )
    invalid_phase = client.put(
        "/api/v1/workers/worker-a/axiom-tools",
        json={"bootstrap": [], "reason": ["http_request"], "explore": []},
    )
    assert invalid_phase.status_code == 409, invalid_phase.text

    saved = client.put(
        "/api/v1/workers/worker-a/axiom-tools",
        json={"bootstrap": ["search_text"], "reason": [], "explore": ["search_text"]},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["bootstrap"] == ["search_text"]
    assert saved.json()["effective"]["bootstrap"] == []
    assert saved.json()["effective"]["explore"] == ["search_text"]

    disabled = client.patch("/api/v1/axiom-tools/search_text", json={"enabled": False})
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["bindings"]
    overview = client.get("/api/v1/workers/overview")
    assert overview.status_code == 200, overview.text
    worker = next(item for item in overview.json() if item["name"] == "worker-a")
    assert worker["axiom_tool_counts"] == {"bootstrap": 1, "reason": 0, "explore": 1}
    enabled = client.patch("/api/v1/axiom-tools/search_text", json={"enabled": True})
    assert enabled.status_code == 200


def test_worker_overview_redacts_pi_secrets(client) -> None:
    client.put(
        "/api/v1/workers/secret-worker",
        json={
            "capabilities": ["explore"],
            "config": {
                "model": "private-model",
                "base_url": "https://models.example/v1",
                "api_key": "super-secret",
                "environment": {"PRIVATE_TOKEN": "another-secret"},
            },
        },
    )

    response = client.get("/api/v1/workers/overview")

    assert response.status_code == 200, response.text
    worker = next(item for item in response.json() if item["name"] == "secret-worker")
    assert worker["model"] == "private-model"
    assert "api_key" not in worker["config"]
    assert "PRIVATE_TOKEN" not in json.dumps(worker["config"])


def test_effective_tools_are_snapshotted_per_worker_and_phase(client, tmp_path) -> None:
    client.put(
        "/api/v1/workers/snapshot-worker",
        json={"capabilities": ["bootstrap", "explore"], "config": {"model": "m"}},
    )
    response = client.put(
        "/api/v1/workers/snapshot-worker/axiom-tools",
        json={
            "bootstrap": ["search_text", "http_request"],
            "reason": [],
            "explore": ["run_python"],
        },
    )
    assert response.status_code == 200, response.text

    service = client.app.state.container.axiom_tools
    keys, extensions, snapshot_path = service.snapshot_for_lease(
        worker_name="snapshot-worker",
        phase="bootstrap",
        run_dir=tmp_path / "run",
    )

    assert keys == ["http_request", "search_text"]
    assert [Path(item).parent.name for item in extensions] == ["http_request", "search_text"]
    payload = json.loads(Path(snapshot_path).read_text(encoding="utf-8"))
    assert payload["worker"] == "snapshot-worker"
    assert payload["phase"] == "bootstrap"
    assert [item["key"] for item in payload["tools"]] == keys
    assert all(item["input_schema"]["type"] == "object" for item in payload["tools"])


def test_worker_delete_is_blocked_by_active_reason_run(client, engine) -> None:
    client.put(
        "/api/v1/workers/busy-worker",
        json={"capabilities": ["reason"], "config": {"model": "m"}},
    )
    now = utcnow()
    with engine.begin() as conn:
        conn.execute(
            projects.insert().values(
                id="p_busy",
                title="busy",
                description="",
                status="active",
                created_at=now,
                updated_at=now,
            )
        )
        conn.execute(
            tasks.insert().values(
                id="t_busy",
                project_id="p_busy",
                title="busy",
                origin_host="example.com",
                origin_scheme="https",
                origin_port=None,
                origin_input="https://example.com",
                goal="goal",
                current_goal_cycle_id=None,
                status="running",
                phase="reason",
                scope_json="{}",
                worker_pool_json="[]",
                guard_enabled=False,
                external_report_enabled=False,
                legacy_readonly=False,
                created_at=now,
                updated_at=now,
            )
        )
        conn.execute(
            reason_runs.insert().values(
                id="rr_busy",
                task_id="t_busy",
                status="running",
                trigger="scheduler",
                trigger_payload_json="{}",
                input_fingerprint="fingerprint",
                worker_pool_json="[]",
                worker="busy-worker",
                settings_version=1,
                execute_timeout_seconds=300,
                conclude_timeout_seconds=180,
                target_open_intents=1,
                max_new_intents=1,
                explore_execute_min_seconds=60,
                explore_execute_max_seconds=3600,
                explore_conclude_min_seconds=180,
                explore_conclude_max_seconds=600,
                lease_id="lease_busy",
                retry_count=0,
                infrastructure_retry_count=0,
                last_failure_kind="",
                failure_class="",
                activity_kind="running",
                activity_summary="",
                activity_tool="",
                created_at=now,
                updated_at=now,
            )
        )

    response = client.delete("/api/v1/workers/busy-worker")

    assert response.status_code == 409, response.text
    assert "ReasonRuns" in response.text
