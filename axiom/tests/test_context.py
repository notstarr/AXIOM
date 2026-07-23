from __future__ import annotations

import json
from pathlib import Path


def _create_task(client, *, goal: str = "验证上下文索引不会直接注入完整证据") -> dict:
    project = client.post("/api/v1/projects", json={"title": "上下文测试"}).json()
    return client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "渐进式披露",
            "origin": "a.example.com",
            "goal": goal,
            "hints": ["优先覆盖攻击面，再深入验证"],
        },
    ).json()


def test_context_indexes_are_bounded_and_expand_returns_full_content(client, data_dir) -> None:
    long_origin = "a.example.com/" + "接口路径与参数" * 8
    task = _create_task(
        client,
        goal=long_origin,
    )

    response = client.post(
        f"/api/v1/tasks/{task['id']}/context",
        json={"phase": "bootstrap", "intent_id": "i001"},
    )
    assert response.status_code == 200, response.text
    revision = response.json()
    manifest_path = Path(revision["manifest_path"])
    graph_path = manifest_path.parent / "graph.json"
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    origin = next(item for item in graph["facts"] if item["id"] == "goal")

    assert origin["ref"] == "fact:goal"
    assert origin["preview"].endswith("…")
    assert len(origin["preview"][:-1]) == 20
    assert long_origin not in graph_path.read_text(encoding="utf-8")
    assert manifest_path.is_relative_to(data_dir)

    expanded = client.post(
        f"/api/v1/tasks/{task['id']}/context/expand",
        json={"refs": ["fact:goal", "hint:h001"]},
    )
    assert expanded.status_code == 200, expanded.text
    entries = expanded.json()["entries"]
    assert entries["fact:goal"]["description"] == long_origin
    assert entries["hint:h001"]["content"] == "优先覆盖攻击面，再深入验证"


def test_context_revisions_are_immutable_and_monotonic(client) -> None:
    task = _create_task(client)

    first = client.post(
        f"/api/v1/tasks/{task['id']}/context",
        json={"phase": "bootstrap", "intent_id": "i001"},
    ).json()
    second = client.post(
        f"/api/v1/tasks/{task['id']}/context",
        json={"phase": "bootstrap", "intent_id": "i001"},
    ).json()

    assert (first["revision"], second["revision"]) == (1, 2)
    assert first["manifest_key"] != second["manifest_key"]
    assert Path(first["manifest_path"]).exists()
    assert Path(second["manifest_path"]).exists()
    assert first["content_hash"] != second["content_hash"]


def test_artifact_content_is_only_available_through_expansion(client) -> None:
    task = _create_task(client)
    client.put(
        "/api/v1/workers/worker-a",
        json={"capabilities": ["bootstrap"], "max_running": 1},
    )
    claim = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/intents/i001/claim",
        json={"worker_name": "worker-a"},
    ).json()
    lease = {"lease_id": claim["lease_id"]}
    client.post(f"/api/v1/dispatch/tasks/{task['id']}/intents/i001/start", json=lease)
    completed = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/intents/i001/bootstrap-complete",
        json={
            **lease,
            "summary": "完成攻击面收集",
            "artifacts": {
                "asset_inventory": "# 资产\n\nSECRET-EVIDENCE",
                "initial_leads": "# 线索\n\n验证授权",
                "findings": "# 发现\n\n暂无",
            },
        },
    )
    assert completed.status_code == 200, completed.text
    artifact_id = completed.json()["artifacts"][0]["id"]

    revision = client.post(
        f"/api/v1/tasks/{task['id']}/context",
        json={"phase": "reason", "intent_id": "i002"},
    ).json()
    directory = Path(revision["manifest_path"]).parent
    artifact_index = (directory / "artifacts.json").read_text(encoding="utf-8")
    assert "SECRET-EVIDENCE" not in artifact_index

    expanded = client.post(
        f"/api/v1/tasks/{task['id']}/context/expand",
        json={"refs": [f"artifact:{artifact_id}"]},
    ).json()
    assert "SECRET-EVIDENCE" in expanded["entries"][f"artifact:{artifact_id}"]["content"]
