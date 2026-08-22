from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import update

from axiom.modules.artifacts.tables import artifacts
from axiom.modules.knowledge.tables import project_knowledge
from axiom.modules.tasks.tables import task_hints
from axiom.modules.todos.tables import todos
from protocol_helpers import write_bootstrap_files


def _create_task(
    client,
    *,
    goal: str = "验证上下文索引不会直接注入完整证据",
    scope: dict | None = None,
) -> dict:
    project = client.post("/api/v1/projects", json={"title": "上下文测试"}).json()
    payload = {
        "project_id": project["id"],
        "title": "渐进式披露",
        "origin": "a.example.com",
        "goal": goal,
        "hints": ["优先覆盖攻击面，再深入验证"],
    }
    if scope is not None:
        payload["scope"] = scope
    return client.post(
        "/api/v1/tasks",
        json=payload,
    ).json()


def test_context_manifest_only_discloses_an_effective_scope(client) -> None:
    unscoped = _create_task(client)
    unscoped_revision = client.post(
        f"/api/v1/tasks/{unscoped['id']}/context",
        json={"phase": "bootstrap", "intent_id": "i001"},
    ).json()
    unscoped_manifest = json.loads(
        Path(unscoped_revision["manifest_path"]).read_text(encoding="utf-8")
    )
    assert "scope" not in unscoped_manifest

    scoped = _create_task(
        client,
        scope={"targets": [], "exclude": ["blocked.example.com"]},
    )
    scoped_revision = client.post(
        f"/api/v1/tasks/{scoped['id']}/context",
        json={"phase": "bootstrap", "intent_id": "i001"},
    ).json()
    scoped_manifest = json.loads(
        Path(scoped_revision["manifest_path"]).read_text(encoding="utf-8")
    )
    assert scoped_manifest["scope"] == {
        "enabled": True,
        "targets": [],
        "exclude": ["blocked.example.com"],
    }


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


def test_high_attention_indexes_include_full_active_context_and_status_reason(client) -> None:
    project = client.post("/api/v1/projects", json={"title": "高注意力上下文"}).json()
    active_knowledge = client.post(
        f"/api/v1/projects/{project['id']}/knowledge",
        json={
            "title": "统一认证约束",
            "content": "登录态由认证域签发，业务域只负责消费令牌。",
            "category": "authentication",
            "created_by": "human",
        },
    ).json()
    disabled_knowledge = client.post(
        f"/api/v1/projects/{project['id']}/knowledge",
        json={"title": "禁用知识", "content": "不得进入上下文", "enabled": False},
    ).json()
    archived_knowledge = client.post(
        f"/api/v1/projects/{project['id']}/knowledge",
        json={"title": "归档知识", "content": "不得进入上下文"},
    ).json()
    client.patch(
        f"/api/v1/projects/{project['id']}/knowledge/{archived_knowledge['id']}",
        json={"archived": True},
    )

    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "高注意力快照",
            "origin": "a.example.com",
            "hints": ["只验证业务域，不扩大资产范围"],
        },
    ).json()
    archived_hint = client.post(
        f"/api/v1/tasks/{task['id']}/hints",
        json={"content": "已失效提示", "creator": "human"},
    ).json()
    client.patch(
        f"/api/v1/tasks/{task['id']}/hints/{archived_hint['id']}",
        json={"archived": True},
    )

    finding = client.post(
        f"/api/v1/tasks/{task['id']}/artifacts/upload",
        files={"file": ("findings.md", b"# Findings\n\nIDOR", "text/markdown")},
        data={"actor": "human", "summary": "人工 Findings"},
    ).json()
    evidence = client.post(
        f"/api/v1/tasks/{task['id']}/artifacts/upload",
        files={"file": ("response.txt", b"HTTP/1.1 200 OK", "text/plain")},
        data={"actor": "human", "summary": "响应证据"},
    ).json()
    vulnerability = client.post(
        f"/api/v1/tasks/{task['id']}/vulnerabilities",
        json={
            "title": "跨组织订单读取",
            "type": "越权访问",
            "severity": "high",
            "description": "普通用户读取了其他组织订单",
            "prerequisites": "已登录普通用户",
            "impact": "跨组织数据泄露",
            "remediation": "校验订单所属组织",
            "affected_target": "a.example.com/api/orders/1",
            "finding_artifact_id": finding["id"],
            "evidence_artifact_ids": [evidence["id"]],
            "source": "explore",
            "reported_by": "worker-a",
        },
    ).json()
    client.patch(
        f"/api/v1/tasks/{task['id']}/vulnerabilities/{vulnerability['id']}",
        json={"status": "忽略", "status_reason": "当前账号本就属于目标组织"},
    )
    archived_vulnerability = client.post(
        f"/api/v1/tasks/{task['id']}/vulnerabilities",
        json={
            "title": "已归档漏洞",
            "type": "信息泄露",
            "severity": "low",
            "description": "历史记录",
            "impact": "无",
            "affected_target": "a.example.com/old",
            "finding_artifact_id": finding["id"],
            "evidence_artifact_ids": [evidence["id"]],
            "source": "human",
            "reported_by": "human",
        },
    ).json()
    client.delete(
        f"/api/v1/tasks/{task['id']}/vulnerabilities/{archived_vulnerability['id']}"
    )

    revision = client.post(
        f"/api/v1/tasks/{task['id']}/context",
        json={"phase": "bootstrap", "intent_id": "i001"},
    ).json()
    directory = Path(revision["manifest_path"]).parent
    hints = json.loads((directory / "hints.json").read_text(encoding="utf-8"))
    knowledge = json.loads((directory / "knowledge.json").read_text(encoding="utf-8"))
    vulnerabilities = json.loads(
        (directory / "vulnerabilities.json").read_text(encoding="utf-8")
    )

    assert hints == [
        {
            "id": "h001",
            "content": "只验证业务域，不扩大资产范围",
            "creator": "human",
            "ref": "hint:h001",
            "detail": hints[0]["detail"],
        }
    ]
    assert knowledge == [
        {
            "id": active_knowledge["id"],
            "title": "统一认证约束",
            "category": "authentication",
            "content": "登录态由认证域签发，业务域只负责消费令牌。",
            "created_by": "human",
            "ref": f"knowledge:{active_knowledge['id']}",
            "detail": knowledge[0]["detail"],
        }
    ]
    assert disabled_knowledge["id"] not in {item["id"] for item in knowledge}
    assert vulnerabilities == [
        {
            "id": vulnerability["id"],
            "severity": "high",
            "status": "忽略",
            "title": "跨组织订单读取",
            "type": "越权访问",
            "affected_target": "a.example.com/api/orders/1",
            "source": "explore",
            "intent_id": None,
            "status_reason": "当前账号本就属于目标组织",
            "ref": f"vulnerability:{vulnerability['id']}",
            "detail": vulnerabilities[0]["detail"],
        }
    ]

    expanded = client.post(
        f"/api/v1/tasks/{task['id']}/context/expand",
        json={"refs": [f"knowledge:{active_knowledge['id']}", f"vulnerability:{vulnerability['id']}"]},
    )
    assert expanded.status_code == 200, expanded.text
    assert expanded.json()["entries"][f"knowledge:{active_knowledge['id']}"]["content"] == (
        "登录态由认证域签发，业务域只负责消费令牌。"
    )
    assert expanded.json()["entries"][f"vulnerability:{vulnerability['id']}"]["status_reason"] == (
        "当前账号本就属于目标组织"
    )


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


def test_high_attention_indexes_are_valid_empty_lists(client) -> None:
    project = client.post("/api/v1/projects", json={"title": "空上下文"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "空高注意力列表",
            "origin": "empty.example.com",
        },
    ).json()

    revision = client.post(
        f"/api/v1/tasks/{task['id']}/context",
        json={"phase": "bootstrap", "intent_id": "i001"},
    ).json()
    directory = Path(revision["manifest_path"]).parent

    for filename in ("hints.json", "knowledge.json", "vulnerabilities.json"):
        assert json.loads((directory / filename).read_text(encoding="utf-8")) == []


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
    execution_input = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/intents/i001/input",
        json=lease,
    ).json()
    write_bootstrap_files(
        execution_input["run_dir"],
        {
            "asset_inventory.md": "# 资产\n\nSECRET-EVIDENCE",
            "initial_leads.md": "# 线索\n\n验证授权",
            "findings.md": "# 发现\n\n暂无",
        },
    )
    completed = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/intents/i001/bootstrap-complete",
        json=lease,
    )
    assert completed.status_code == 200, completed.text
    artifact_id = completed.json()["artifacts"][0]["id"]

    revision = client.post(
        f"/api/v1/tasks/{task['id']}/context",
        json={
            "phase": "reason",
            "reason_run_id": completed.json()["next_reason_run_id"],
        },
    )
    assert revision.status_code == 200, revision.text
    revision = revision.json()
    directory = Path(revision["manifest_path"]).parent
    graph = json.loads((directory / "graph.json").read_text(encoding="utf-8"))
    bootstrap_fact = next(item for item in graph["facts"] if item["id"] == "f001")
    assert bootstrap_fact["source_intent_ids"] == ["i001"]
    assert artifact_id in bootstrap_fact["source_artifact_ids"]
    assert bootstrap_fact["assessment_status"] == "not_applicable"
    assert bootstrap_fact["consumption_status"] == "reserved"
    assert bootstrap_fact["consumed_by_reason_runs"][0]["reason_run_id"] == completed.json()["next_reason_run_id"]
    artifact_index = (directory / "artifacts.json").read_text(encoding="utf-8")
    assert "SECRET-EVIDENCE" not in artifact_index

    expanded = client.post(
        f"/api/v1/tasks/{task['id']}/context/expand",
        json={"refs": [f"artifact:{artifact_id}"]},
    ).json()
    assert "SECRET-EVIDENCE" in expanded["entries"][f"artifact:{artifact_id}"]["content"]


def test_plan_context_reuses_existing_indexes_and_includes_archived_history(
    client,
    engine,
) -> None:
    task = _create_task(client)
    knowledge = client.post(
        f"/api/v1/projects/{task['project_id']}/knowledge",
        json={
            "title": "历史凭证关系",
            "content": "历史 Token 可用于另一模块。",
            "category": "credential",
            "created_by": "human",
        },
    ).json()
    artifact = client.post(
        f"/api/v1/tasks/{task['id']}/artifacts/upload",
        files={"file": ("leads.md", b"# Leads\n\narchived-plan-lead", "text/markdown")},
        data={"actor": "human", "summary": "历史 Lead"},
    ).json()
    todo_response = client.post(
        f"/api/v1/tasks/{task['id']}/todos",
        json={
            "title": "重新复核历史 Lead",
            "category": "other",
            "sources": [{"type": "artifact", "id": artifact["id"]}],
        },
    )
    assert todo_response.status_code == 201, todo_response.text
    todo = todo_response.json()

    with engine.begin() as conn:
        conn.execute(
            update(artifacts)
            .where(artifacts.c.task_id == task["id"], artifacts.c.id == artifact["id"])
            .values(archived=True)
        )
        conn.execute(
            update(todos)
            .where(todos.c.task_id == task["id"], todos.c.id == todo["id"])
            .values(
                status="obsolete",
                status_reason="上一轮判断没有可利用路径",
                archived=True,
            )
        )
        conn.execute(
            update(task_hints)
            .where(task_hints.c.task_id == task["id"], task_hints.c.id == "h001")
            .values(archived=True)
        )
        conn.execute(
            update(project_knowledge)
            .where(
                project_knowledge.c.project_id == task["project_id"],
                project_knowledge.c.id == knowledge["id"],
            )
            .values(archived=True)
        )

    reason_revision = client.post(
        f"/api/v1/tasks/{task['id']}/context",
        json={"phase": "reason"},
    ).json()
    plan_revision = client.post(
        f"/api/v1/tasks/{task['id']}/context",
        json={"phase": "plan"},
    ).json()
    reason_dir = Path(reason_revision["manifest_path"]).parent
    plan_dir = Path(plan_revision["manifest_path"]).parent
    reason_manifest = json.loads((reason_dir / "manifest.json").read_text(encoding="utf-8"))
    plan_manifest = json.loads((plan_dir / "manifest.json").read_text(encoding="utf-8"))

    core_indexes = {
        "graph",
        "todos",
        "artifacts",
        "hints",
        "knowledge",
        "vulnerabilities",
    }
    assert core_indexes <= set(reason_manifest["indexes"])
    assert set(plan_manifest["indexes"]) == core_indexes | {
        "goal_evaluations",
        "goal_cycles",
    }
    assert not {
        "attack_surface.json",
        "unresolved_leads.json",
        "credentials_vault.json",
        "todos_history.json",
    } & {path.name for path in plan_dir.iterdir()}

    assert json.loads((reason_dir / "todos.json").read_text(encoding="utf-8")) == []
    plan_todos = json.loads((plan_dir / "todos.json").read_text(encoding="utf-8"))
    plan_todo = next(item for item in plan_todos if item["id"] == todo["id"])
    assert plan_todo["status"] == "obsolete"
    assert plan_todo["status_reason"] == "上一轮判断没有可利用路径"
    assert plan_todo["archived"] is True

    plan_artifacts = json.loads((plan_dir / "artifacts.json").read_text(encoding="utf-8"))
    plan_artifact = next(item for item in plan_artifacts if item["id"] == artifact["id"])
    artifact_detail = json.loads(
        (plan_dir / plan_artifact["detail"]).read_text(encoding="utf-8")
    )
    assert artifact_detail["archived"] is True
    assert artifact_detail["content_path"].endswith("leads.md")
    assert knowledge["id"] in {
        item["id"]
        for item in json.loads((plan_dir / "knowledge.json").read_text(encoding="utf-8"))
    }
    assert "h001" in {
        item["id"]
        for item in json.loads((plan_dir / "hints.json").read_text(encoding="utf-8"))
    }

    expanded_plan = client.post(
        f"/api/v1/tasks/{task['id']}/context/expand",
        json={
            "phase": "plan",
            "refs": [f"todo:{todo['id']}", f"artifact:{artifact['id']}"],
        },
    )
    assert expanded_plan.status_code == 200, expanded_plan.text
    assert expanded_plan.json()["entries"][f"todo:{todo['id']}"]["archived"] is True
    assert "archived-plan-lead" in expanded_plan.json()["entries"][
        f"artifact:{artifact['id']}"
    ]["content"]
