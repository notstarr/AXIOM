from __future__ import annotations

import json
from pathlib import Path
import shutil

import pytest
from sqlalchemy import update

from axiom.modules.graph.tables import intents
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
                "supported_phases": phases
                or ["bootstrap", "reason", "explore", "plan", "review"],
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


def _scan_managed_tool(client, data_dir: Path, key: str, *, phases=None) -> None:
    _write_tool(data_dir / "axiom-tools", key, phases=phases)
    response = client.post("/api/v1/axiom-tools/scan")
    assert response.status_code == 200, response.text


def test_placeholder_bundled_tools_are_no_longer_discovered(client) -> None:
    response = client.get("/api/v1/axiom-tools")

    assert response.status_code == 200, response.text
    items = response.json()
    retired = {
        "search_text",
        "find_files",
        "list_directory",
        "http_request",
        "run_python",
    }
    assert not ({item["key"] for item in items} & retired)
    assert items == []


def test_scan_is_idempotent_and_preserves_missing_bindings(client, data_dir) -> None:
    root = data_dir / "axiom-tools"
    package = _write_tool(root, "managed_demo", phases=["explore"])

    first = client.post("/api/v1/axiom-tools/scan")
    second = client.post("/api/v1/axiom-tools/scan")
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    item = next(item for item in second.json()["items"] if item["key"] == "managed_demo")
    assert item["status"] == "ready"

    bound = client.put(
        "/api/v1/phase-capabilities/axiom_tool/managed_demo",
        json={"scopes": ["explore"]},
    )
    assert bound.status_code == 200, bound.text
    shutil.rmtree(package)
    missing = client.post("/api/v1/axiom-tools/scan")
    assert missing.status_code == 200, missing.text
    item = client.get("/api/v1/axiom-tools/managed_demo").json()
    assert item["status"] == "missing"
    assert item["scopes"] == ["explore"]


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


def test_reserved_duplicate_and_symlink_packages_are_rejected(
    client, data_dir, monkeypatch
) -> None:
    root = data_dir / "axiom-tools"
    bundled_root = data_dir / "bundled-tools"
    monkeypatch.setattr(
        client.app.state.container.axiom_tools,
        "bundled_root",
        bundled_root,
    )
    _write_tool(root, "read")
    _write_tool(root, "duplicate_probe")
    _write_tool(bundled_root, "duplicate_probe")
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
    duplicate = client.get("/api/v1/axiom-tools/duplicate_probe").json()
    assert duplicate["status"] == "invalid"
    assert "重复" in duplicate["status_error"]
    linked = next(item for item in response.json()["items"] if item["key"] == "linked_tool")
    assert linked["status"] == "invalid"


def test_binding_is_validated_and_global_disable_keeps_effective_state(
    client, data_dir
) -> None:
    _scan_managed_tool(client, data_dir, "shared_probe")
    _scan_managed_tool(client, data_dir, "explore_probe", phases=["explore"])
    client.put(
        "/api/v1/workers/worker-a",
        json={"capabilities": ["explore"], "config": {"model": "m"}},
    )
    client.put(
        "/api/v1/workers/worker-b",
        json={"capabilities": ["bootstrap", "reason", "explore"], "config": {"model": "m"}, "disabled": True},
    )
    invalid_phase = client.put(
        "/api/v1/phase-capabilities/axiom_tool/explore_probe",
        json={"scopes": ["reason"]},
    )
    assert invalid_phase.status_code == 409, invalid_phase.text

    saved = client.put(
        "/api/v1/phase-capabilities/axiom_tool/shared_probe",
        json={"scopes": ["bootstrap", "explore"]},
    )
    assert saved.status_code == 200, saved.text
    item = next(
        item
        for item in saved.json()["items"]
        if item["capability_key"] == "shared_probe"
    )
    assert item["scopes"] == ["bootstrap", "explore"]

    disabled = client.patch("/api/v1/axiom-tools/shared_probe", json={"enabled": False})
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["scopes"] == ["bootstrap", "explore"]
    matrix = client.get("/api/v1/phase-capabilities").json()
    disabled_item = next(
        item
        for item in matrix["items"]
        if item["capability_key"] == "shared_probe"
    )
    assert disabled_item["inactive_reasons"] == {
        "bootstrap": "工具已全局停用",
        "explore": "工具已全局停用",
    }
    overview = client.get("/api/v1/workers/overview")
    assert overview.status_code == 200, overview.text
    worker = next(item for item in overview.json() if item["name"] == "worker-a")
    assert worker["axiom_tool_counts"] == {
        "bootstrap": 0,
        "reason": 0,
        "explore": 1,
        "plan": 0,
        "review": 0,
    }
    enabled = client.patch("/api/v1/axiom-tools/shared_probe", json={"enabled": True})
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


def test_worker_overview_counts_active_plan_run(client, engine) -> None:
    project = client.post("/api/v1/projects", json={"title": "Plan worker overview"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "Plan worker overview",
            "origin": "a.example.com",
            "goal": "exercise the Plan worker overview",
            "worker_pool": ["overview-plan-worker"],
        },
    ).json()
    worker = client.put(
        "/api/v1/workers/overview-plan-worker",
        json={"capabilities": ["plan"], "max_running": 1, "config": {"model": "m"}},
    )
    assert worker.status_code == 200, worker.text
    now = utcnow()
    with engine.begin() as conn:
        conn.execute(
            update(intents)
            .where(intents.c.task_id == task["id"], intents.c.id == "i001")
            .values(status="completed", concluded_at=now)
        )
        conn.execute(
            update(tasks)
            .where(tasks.c.id == task["id"])
            .values(phase="waiting", updated_at=now)
        )
    triggered = client.post(
        f"/api/v1/tasks/{task['id']}/plan-runs/trigger",
        json={"trigger": "manual"},
    )
    assert triggered.status_code == 200, triggered.text
    plan_run_id = triggered.json()["id"]
    claim = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/plan-runs/{plan_run_id}/claim",
        json={"worker_name": "overview-plan-worker"},
    )
    assert claim.status_code == 200, claim.text

    overview = client.get("/api/v1/workers/overview")
    assert overview.status_code == 200, overview.text
    overview_worker = next(
        item for item in overview.json() if item["name"] == "overview-plan-worker"
    )
    assert overview_worker["running"] == 1


def test_effective_tools_are_snapshotted_per_worker_and_phase(
    client, data_dir, tmp_path
) -> None:
    _scan_managed_tool(client, data_dir, "bootstrap_probe", phases=["bootstrap"])
    _scan_managed_tool(client, data_dir, "shared_probe")
    _scan_managed_tool(client, data_dir, "explore_probe", phases=["explore"])
    client.put(
        "/api/v1/workers/snapshot-worker",
        json={"capabilities": ["bootstrap", "explore"], "config": {"model": "m"}},
    )
    for key, scopes in (
        ("bootstrap_probe", ["bootstrap"]),
        ("shared_probe", ["bootstrap"]),
        ("explore_probe", ["explore"]),
    ):
        response = client.put(
            f"/api/v1/phase-capabilities/axiom_tool/{key}",
            json={"scopes": scopes},
        )
        assert response.status_code == 200, response.text

    service = client.app.state.container.axiom_tools
    keys, extensions, snapshot_path = service.snapshot_for_lease(
        worker_name="snapshot-worker",
        phase="bootstrap",
        run_dir=tmp_path / "run",
    )

    assert keys == ["bootstrap_probe", "shared_probe"]
    assert [Path(item).parent.name for item in extensions] == [
        "bootstrap_probe",
        "shared_probe",
    ]
    payload = json.loads(Path(snapshot_path).read_text(encoding="utf-8"))
    assert payload["worker"] == "snapshot-worker"
    assert payload["phase"] == "bootstrap"
    assert [item["key"] for item in payload["tools"]] == keys
    assert all(item["input_schema"]["type"] == "object" for item in payload["tools"])

    capability_payload = json.loads(
        (tmp_path / "run" / "phase-capabilities.json").read_text(encoding="utf-8")
    )
    assert capability_payload["schema_version"] == 2
    assert not {
        "search_text",
        "find_files",
        "list_directory",
        "http_request",
        "run_python",
    } & {item["key"] for item in capability_payload["axiom_tools"]}


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
