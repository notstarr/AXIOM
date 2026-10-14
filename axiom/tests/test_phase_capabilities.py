from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from axiom.modules.phase_capabilities.service import PhaseCapabilityService
from axiom.shared.errors import ConflictError


RETIRED_PLACEHOLDER_TOOLS = {
    "search_text",
    "find_files",
    "list_directory",
    "http_request",
    "run_python",
}


def _put_worker(client, name: str, capabilities: list[str]) -> None:
    response = client.put(
        f"/api/v1/workers/{name}",
        json={"capabilities": capabilities, "config": {"model": "test-model"}},
    )
    assert response.status_code == 200, response.text


def _item(body: dict, kind: str, key: str) -> dict:
    return next(
        item
        for item in body["items"]
        if item["capability_kind"] == kind and item["capability_key"] == key
    )


def _write_extension_tool(
    root: Path,
    key: str,
    *,
    phases: list[str] | None = None,
) -> None:
    package = root / key
    package.mkdir(parents=True, exist_ok=True)
    (package / "tool.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "key": key,
                "title": key,
                "version": "1.0.0",
                "description": "phase capability test tool",
                "entrypoint": "index.ts",
                "supported_phases": phases
                or ["bootstrap", "reason", "explore", "plan", "review"],
                "risk_tags": ["test"],
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                "prompt_guidelines": [],
            }
        ),
        encoding="utf-8",
    )
    (package / "index.ts").write_text(
        "export default function phaseTestTool(pi) { void pi; }\n",
        encoding="utf-8",
    )


def _scan_extension_tool(
    client,
    data_dir: Path,
    key: str,
    *,
    phases: list[str] | None = None,
) -> None:
    _write_extension_tool(data_dir / "axiom-tools", key, phases=phases)
    response = client.post("/api/v1/axiom-tools/scan")
    assert response.status_code == 200, response.text


def test_five_logical_worker_phases_share_model_backends(client) -> None:
    _put_worker(client, "shared-backend", ["bootstrap", "reason"])

    phases = client.get("/api/v1/worker-phases")
    assert phases.status_code == 200, phases.text
    body = phases.json()
    assert [item["phase"] for item in body] == [
        "bootstrap",
        "reason",
        "explore",
        "plan",
        "review",
    ]
    assert [item["title"] for item in body] == [
        "Bootstrap Worker",
        "Reason Worker",
        "Explore Worker",
        "Plan Worker",
        "Reviewer Worker",
    ]
    assert [item["name"] for item in body[0]["backends"]] == ["shared-backend"]
    assert [item["name"] for item in body[1]["backends"]] == ["shared-backend"]
    assert body[2]["backends"] == []
    assert body[3]["backends"] == []

    saved = client.put(
        "/api/v1/worker-phases/explore/backends",
        json={"backends": ["shared-backend"]},
    )
    assert saved.status_code == 200, saved.text
    worker = client.get("/api/v1/workers/shared-backend").json()
    assert worker["capabilities"] == ["bootstrap", "reason", "explore"]


def test_phase_running_counts_are_phase_specific_but_capacity_is_shared(client) -> None:
    project = client.post("/api/v1/projects", json={"title": "阶段运行量"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "阶段运行量任务",
            "origin": "phase-count.example.com",
            "goal": "验证阶段运行量",
            "worker_pool": ["phase-count-backend"],
        },
    ).json()
    _put_worker(client, "phase-count-backend", ["bootstrap", "reason", "explore"])
    claim = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/intents/i001/claim",
        json={"worker_name": "phase-count-backend"},
    )
    assert claim.status_code == 200, claim.text

    phases = client.get("/api/v1/worker-phases").json()
    by_phase = {item["phase"]: item for item in phases}
    for phase in ("bootstrap", "reason", "explore"):
        backend = next(
            item
            for item in by_phase[phase]["backends"]
            if item["name"] == "phase-count-backend"
        )
        assert backend["shared_running"] == 1
        assert backend["available"] == 0
    assert by_phase["bootstrap"]["running"] == 1
    assert by_phase["reason"]["running"] == 0
    assert by_phase["explore"]["running"] == 0

    assert client.post(f"/api/v1/tasks/{task['id']}/stop").status_code == 200


def test_common_and_phase_specific_capabilities_do_not_leak(client, data_dir) -> None:
    _scan_extension_tool(client, data_dir, "common_probe")
    _scan_extension_tool(client, data_dir, "explore_probe", phases=["explore"])
    common = client.put(
        "/api/v1/phase-capabilities/axiom_tool/common_probe",
        json={"scopes": ["common"]},
    )
    assert common.status_code == 200, common.text
    explore = client.put(
        "/api/v1/phase-capabilities/axiom_tool/explore_probe",
        json={"scopes": ["explore"]},
    )
    assert explore.status_code == 200, explore.text

    service = client.app.state.container.phase_capabilities
    assert "common_probe" in service.effective_keys(
        phase="bootstrap", kind="axiom_tool"
    )
    assert "common_probe" in service.effective_keys(
        phase="reason", kind="axiom_tool"
    )
    assert "common_probe" in service.effective_keys(
        phase="explore", kind="axiom_tool"
    )
    assert "common_probe" in service.effective_keys(
        phase="review", kind="axiom_tool"
    )
    assert "explore_probe" not in service.effective_keys(
        phase="bootstrap", kind="axiom_tool"
    )
    assert "explore_probe" not in service.effective_keys(
        phase="reason", kind="axiom_tool"
    )
    assert "explore_probe" in service.effective_keys(
        phase="explore", kind="axiom_tool"
    )
    assert "explore_probe" not in service.effective_keys(
        phase="review", kind="axiom_tool"
    )


def test_common_axiom_tool_must_support_all_five_phases(client, data_dir) -> None:
    _scan_extension_tool(client, data_dir, "bootstrap_probe", phases=["bootstrap"])
    response = client.put(
        "/api/v1/phase-capabilities/axiom_tool/bootstrap_probe",
        json={"scopes": ["common"]},
    )
    assert response.status_code == 409, response.text
    assert "五个阶段" in response.text or "bootstrap" in response.text


def test_common_scope_canonicalizes_redundant_phase_assignments(
    client, data_dir
) -> None:
    _scan_extension_tool(client, data_dir, "canonical_probe")

    response = client.put(
        "/api/v1/phase-capabilities/axiom_tool/canonical_probe",
        json={"scopes": ["explore", "common", "bootstrap"]},
    )

    assert response.status_code == 200, response.text
    item = _item(response.json(), "axiom_tool", "canonical_probe")
    assert item["scopes"] == ["common"]


def test_builtin_tool_catalog_matches_fixed_stage_contracts_and_is_read_only(
    client,
) -> None:
    response = client.get("/api/v1/phase-capabilities")

    assert response.status_code == 200, response.text
    body = response.json()
    tools = {item["key"]: item for item in body["builtin_tools"]}
    assert {"read", "write", "edit", "bash"}.issubset(tools)
    assert {
        "axiom.skill.path",
        "axiom.skill.doctor",
        "axiom.credential.list",
        "axiom.credential.get",
        "axiom.credential.refresh",
        "axiom.reason.status",
        "axiom.reason.todo.add",
        "axiom.reason.todo.update",
        "axiom.reason.intent.propose",
        "axiom.reason.goal.assess",
        "axiom.plan.status",
        "axiom.plan.todo.add",
        "axiom.plan.todo.update",
        "axiom.plan.todo.delete",
        "axiom.plan.intent.propose",
        "axiom.plan.decision.submit",
        "axiom.plan.exhaustion.submit",
        "axiom.explore.vulnerability.report",
        "axiom.explore.human_intervention.mark",
    }.issubset(tools)
    assert tools["bash"]["availability"] == {
        "bootstrap": ["execute"],
        "reason": ["execute", "conclude"],
        "explore": ["execute", "conclude"],
        "plan": ["execute", "conclude"],
        "review": ["execute", "conclude"],
    }
    assert tools["axiom.reason.status"]["phases"] == ["reason"]
    assert tools["axiom.plan.status"]["phases"] == ["plan"]
    assert tools["axiom.explore.vulnerability.report"]["phases"] == ["explore"]
    assert tools["axiom.explore.human_intervention.mark"]["phases"] == ["explore"]
    assert tools["axiom.explore.human_intervention.mark"]["availability"] == {
        "explore": ["execute", "conclude"],
    }
    assert tools["axiom.review.status"]["phases"] == ["review"]
    assert tools["axiom.review.decision.submit"]["phases"] == ["review"]
    browser_tools = {
        "browser_open_page",
        "browser_list_pages",
        "browser_close_page",
        "browser_navigate",
        "browser_read_page",
        "browser_snapshot",
        "browser_click",
        "browser_fill",
        "browser_press",
        "browser_hover",
        "browser_scroll",
        "browser_select",
        "browser_check",
        "browser_upload_file",
        "browser_wait",
        "browser_screenshot",
        "browser_read_network",
        "browser_read_console",
        "browser_evaluate",
    }
    assert browser_tools <= set(tools)
    assert all(tools[name]["source"] == "axiom_browser" for name in browser_tools)
    assert all(
        tools[name]["phases"] == ["bootstrap", "explore"]
        for name in browser_tools
    )
    assert all(tools[name]["locked"] is True for name in browser_tools)
    assert not (browser_tools & {item["capability_key"] for item in body["items"]})
    assert all(item["locked"] is True for item in tools.values())
    assert not (RETIRED_PLACEHOLDER_TOOLS & set(tools))
    assert not (
        RETIRED_PLACEHOLDER_TOOLS
        & {item["capability_key"] for item in body["items"]}
    )

    rejected = client.put(
        "/api/v1/phase-capabilities/builtin_tool/read",
        json={"scopes": ["explore"]},
    )
    assert rejected.status_code == 409, rejected.text
    assert "Invalid capability kind" in rejected.text


def test_plan_builtin_tools_are_independent_from_reason(client) -> None:
    body = client.get("/api/v1/phase-capabilities").json()
    tools = {item["key"]: item for item in body["builtin_tools"]}
    plan_tools = {
        "axiom.plan.status",
        "axiom.plan.todo.add",
        "axiom.plan.todo.update",
        "axiom.plan.todo.delete",
        "axiom.plan.intent.propose",
        "axiom.plan.decision.submit",
        "axiom.plan.exhaustion.submit",
    }

    assert plan_tools.issubset(tools)
    assert all(tools[key]["phases"] == ["plan"] for key in plan_tools)
    assert all(
        tools[key]["availability"] == {"plan": ["execute", "conclude"]}
        for key in plan_tools
    )
    assert tools["axiom.reason.status"]["phases"] == ["reason"]
    assert tools["axiom.plan.status"]["invocation"] == "axiom plan status"


def test_locked_whatisvul_is_common_and_cannot_be_removed(client) -> None:
    body = client.get("/api/v1/phase-capabilities").json()
    item = _item(body, "skill", "whatisvul")
    assert item["scopes"] == ["common"]
    assert item["locked_scopes"] == ["common"]

    removed = client.put(
        "/api/v1/phase-capabilities/skill/whatisvul",
        json={"scopes": []},
    )
    assert removed.status_code == 409, removed.text

    disabled = client.patch("/api/v1/skills/whatisvul/enabled", json={"enabled": False})
    assert disabled.status_code == 409, disabled.text


def test_vulnerability_review_skill_defaults_only_to_reviewer(client) -> None:
    body = client.get("/api/v1/phase-capabilities").json()
    item = _item(body, "skill", "vuln-review")

    assert item["scopes"] == ["review"]
    service = client.app.state.container.phase_capabilities
    assert "vuln-review" in service.effective_keys(phase="review", kind="skill")
    for phase in ("bootstrap", "reason", "explore"):
        assert "vuln-review" not in service.effective_keys(phase=phase, kind="skill")


def test_skill_snapshot_is_filtered_by_phase(client, tmp_path: Path) -> None:
    _put_worker(client, "skill-backend", ["bootstrap", "explore"])
    saved = client.put(
        "/api/v1/phase-capabilities/skill/browser-control",
        json={"scopes": ["explore"]},
    )
    assert saved.status_code == 200, saved.text

    service = client.app.state.container.phase_capabilities
    bootstrap = service.snapshot_for_lease(
        worker_name="skill-backend",
        phase="bootstrap",
        run_dir=tmp_path / "bootstrap",
    )
    explore = service.snapshot_for_lease(
        worker_name="skill-backend",
        phase="explore",
        run_dir=tmp_path / "explore",
    )
    assert not any("browser-control" in path for path in bootstrap.skill_directories)
    assert any("browser-control" in path for path in explore.skill_directories)
    payload = json.loads(
        Path(explore.capability_snapshot_path).read_text(encoding="utf-8")
    )
    assert payload["schema_version"] == 3
    assert payload["worker_backend"] == "skill-backend"
    assert payload["phase"] == "explore"
    assert payload["builtin_tools"]
    assert all("explore" in item["phases"] for item in payload["builtin_tools"])
    assert not (
        RETIRED_PLACEHOLDER_TOOLS
        & {item["key"] for item in payload["axiom_tools"]}
    )


def test_pi_native_mcp_names_match_pi_sanitization_and_collision_hash() -> None:
    rows = [
        {"id": "one", "server_id": "server", "server_name": "my-server", "name": "do-thing"},
        {"id": "two", "server_id": "server", "server_name": "my-server", "name": "do_thing"},
    ]

    names = PhaseCapabilityService._native_mcp_names(rows)
    for row in rows:
        base = f"mcp__{row['server_name']}__{row['name']}".replace("-", "_")
        suffix = hashlib.sha256(
            f"{row['server_name']}\0{row['name']}".encode()
        ).hexdigest()[:8]
        assert names[row["id"]] == f"{base[:55]}_{suffix}"
    assert names["server:server"] == "my-server"


def test_pi_native_mcp_rejects_legacy_sse_runtime_config() -> None:
    row = {
        "name": "legacy",
        "server_name": "legacy",
        "transport": "sse",
        "url": "https://mcp.example/sse",
        "command": "",
        "args_json": "[]",
        "env_json": "{}",
        "raw_config_json": "{}",
    }
    with pytest.raises(ConflictError, match="SSE"):
        PhaseCapabilityService._mcp_runtime_config(row)
    assert PhaseCapabilityService._mcp_runtime_config(row, allow_sse=True)["transport"] == "sse"


def test_new_skill_snapshot_hash_tracks_support_file_updates(client, tmp_path: Path) -> None:
    created = client.post(
        "/api/v1/skills",
        json={
            "name": "snapshot-package",
            "description": "snapshot package",
            "content": "读取 references/guide.md。",
        },
    )
    assert created.status_code == 201, created.text
    support = client.post(
        "/api/v1/skills/snapshot-package/files",
        json={"path": "references/guide.md", "content": "版本一"},
    )
    assert support.status_code == 200, support.text
    binding = client.put(
        "/api/v1/phase-capabilities/skill/snapshot-package",
        json={"scopes": ["explore"]},
    )
    assert binding.status_code == 200, binding.text
    _put_worker(client, "skill-hash-backend", ["explore"])

    service = client.app.state.container.phase_capabilities
    first = service.snapshot_for_lease(
        worker_name="skill-hash-backend",
        phase="explore",
        run_dir=tmp_path / "first",
    )
    first_payload = json.loads(
        Path(first.capability_snapshot_path).read_text(encoding="utf-8")
    )
    first_item = next(
        item for item in first_payload["skills"] if item["key"] == "snapshot-package"
    )

    updated = client.put(
        "/api/v1/skills/snapshot-package/files/references/guide.md",
        json={"content": "版本二"},
    )
    assert updated.status_code == 200, updated.text
    second = service.snapshot_for_lease(
        worker_name="skill-hash-backend",
        phase="explore",
        run_dir=tmp_path / "second",
    )
    second_payload = json.loads(
        Path(second.capability_snapshot_path).read_text(encoding="utf-8")
    )
    second_item = next(
        item for item in second_payload["skills"] if item["key"] == "snapshot-package"
    )

    assert first_item["content_hash"] != second_item["content_hash"]

    same = service.snapshot_for_lease(
        worker_name="skill-hash-backend",
        phase="explore",
        run_dir=tmp_path / "first",
    )
    frozen_payload = json.loads(
        Path(same.capability_snapshot_path).read_text(encoding="utf-8")
    )
    frozen_item = next(
        item for item in frozen_payload["skills"] if item["key"] == "snapshot-package"
    )
    assert frozen_item["content_hash"] == first_item["content_hash"]


def test_lease_snapshot_lists_skills_only_once(client, tmp_path: Path, monkeypatch) -> None:
    service = client.app.state.container.phase_capabilities
    original_list = service.skills.list
    calls = 0

    def counting_list():
        nonlocal calls
        calls += 1
        return original_list()

    monkeypatch.setattr(service.skills, "list", counting_list)
    service.snapshot_for_lease(
        worker_name="reason-backend",
        phase="reason",
        run_dir=tmp_path / "reason-snapshot",
    )

    assert calls == 1


def test_mcp_snapshot_contains_only_selected_tool_and_freezes_server_config(client, tmp_path: Path) -> None:
    server = client.post(
        "/api/v1/mcp/servers",
        json={"name": "snapshot-mcp", "command": "echo", "args": []},
    )
    assert server.status_code == 201, server.text
    server_id = server.json()["id"]
    tools = client.put(
        f"/api/v1/mcp/servers/{server_id}/tools",
        json=[{"name": "selected", "description": "selected"}, {"name": "sibling", "description": "sibling"}],
    )
    assert tools.status_code == 200, tools.text
    selected_id = next(item["id"] for item in tools.json() if item["name"] == "selected")

    binding = client.put(
        f"/api/v1/phase-capabilities/mcp_tool/{selected_id}",
        json={"scopes": ["explore"]},
    )
    assert binding.status_code == 200, binding.text
    _put_worker(client, "mcp-backend", ["explore"])

    service = client.app.state.container.phase_capabilities
    snapshot = service.snapshot_for_lease(
        worker_name="mcp-backend",
        phase="explore",
        run_dir=tmp_path / "run",
    )
    payload = json.loads(Path(snapshot.capability_snapshot_path).read_text(encoding="utf-8"))
    assert [item["tool_name"] for item in payload["mcp_tools"]] == ["selected"]
    assert payload["mcp_tools"][0]["active_name"] in snapshot.mcp_tool_names
    assert payload["mcp_servers"][0]["config"]["command"] == "echo"
    assert all(item["tool_name"] != "sibling" for item in payload["mcp_tools"])

    updated = client.put(
        f"/api/v1/mcp/servers/{server_id}",
        json={"name": "snapshot-mcp", "command": "printf", "args": [], "env": {}, "url": "", "disabled": False, "source_format": "json", "raw_config": {}},
    )
    assert updated.status_code == 200, updated.text
    same_snapshot = service.snapshot_for_lease(
        worker_name="mcp-backend",
        phase="explore",
        run_dir=tmp_path / "run",
    )
    frozen = json.loads(Path(same_snapshot.capability_snapshot_path).read_text(encoding="utf-8"))
    assert frozen["mcp_servers"][0]["config"]["command"] == "echo"
