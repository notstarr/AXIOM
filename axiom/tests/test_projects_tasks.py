from __future__ import annotations

import pytest
from sqlalchemy import event, func, select

from axiom.modules.graph.tables import intent_sources, intents
from axiom.modules.tasks.tables import tasks
from axiom.shared import model_registry as _model_registry  # noqa: F401
from axiom.shared.clock import utcnow
from axiom.shared.database import metadata
from axiom.shared.ids import id_counters


def create_project(client, title: str = "示例系统") -> dict:
    response = client.post("/api/v1/projects", json={"title": title})
    assert response.status_code == 201, response.text
    return response.json()


def test_task_creation_is_atomic_and_seeds_graph(client) -> None:
    project = create_project(client)
    response = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "授权边界测试",
            "origin": "https://API.Example.com:8443",
            "goal": "对该系统进行渗透测试，并挖掘10个高置信度的漏洞",
            "hints": ["优先验证对象级授权", "优先验证对象级授权"],
            "worker_pool": ["worker-a", "worker-a"],
        },
    )
    assert response.status_code == 201, response.text
    task = response.json()
    assert task["id"] == "t_001"
    assert task["origin"] == {
        "host": None,
        "scheme": None,
        "port": None,
        "original_input": "https://API.Example.com:8443",
    }
    assert task["scope"] == {
        "enabled": False,
        "targets": [],
        "exclude": [],
    }
    assert task["worker_pool"] == ["worker-a"]

    hints = client.get(f"/api/v1/tasks/{task['id']}/hints").json()
    assert [(item["id"], item["content"]) for item in hints] == [
        ("h001", "优先验证对象级授权")
    ]

    graph_response = client.get(f"/api/v1/tasks/{task['id']}/graph")
    assert graph_response.status_code == 200
    graph = graph_response.json()
    assert [fact["id"] for fact in graph["facts"]] == ["goal", "origin"]
    assert graph["intents"] == [
        {
            "id": "i001",
            "task_id": task["id"],
            "todo_id": None,
            "kind": "bootstrap",
            "from_facts": ["origin"],
            "description": "bootstrap",
            "status": "pending",
            "creator": "dispatcher.bootstrap",
            "worker": None,
            "worker_pool": ["worker-a"],
            "targets": [],
            "execute_timeout_seconds": None,
            "conclude_timeout_seconds": None,
            "lease_id": None,
            "claimed_at": None,
            "last_heartbeat_at": None,
            "retry_count": 0,
            "infrastructure_retry_count": 0,
            "last_failure_kind": "",
            "failure_class": "",
            "activity_kind": "",
            "activity_summary": "",
            "activity_tool": "",
            "activity_started_at": None,
            "activity_updated_at": None,
            "next_retry_at": None,
            "to_fact_id": None,
            "human_intervention_required": False,
            "human_intervention_reason": "",
            "human_intervention_marked_at": None,
            "created_at": graph["intents"][0]["created_at"],
            "concluded_at": None,
            "archived": False,
        }
    ]


def test_graph_query_count_does_not_grow_with_intent_count(client, engine) -> None:
    project = create_project(client)
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "图谱批量查询",
            "origin": "测试入口",
            "goal": "验证图谱查询不会随 Intent 数量线性增长",
        },
    ).json()
    now = utcnow()
    rows = [
        {
            "id": f"i{index:03d}",
            "task_id": task["id"],
            "goal_cycle_id": task["current_goal_cycle_id"],
            "todo_id": None,
            "kind": "explore",
            "description": f"批量 Intent {index}",
            "status": "pending",
            "creator": "test",
            "worker": None,
            "worker_pool_json": "[]",
            "targets_json": "[]",
            "retry_count": 0,
            "infrastructure_retry_count": 0,
            "last_failure_kind": "",
            "failure_class": "",
            "activity_kind": "",
            "activity_summary": "",
            "activity_tool": "",
            "created_at": now,
            "archived": False,
        }
        for index in range(2, 32)
    ]
    with engine.begin() as conn:
        conn.execute(intents.insert(), rows)
        conn.execute(
            intent_sources.insert(),
            [
                {
                    "task_id": task["id"],
                    "intent_id": row["id"],
                    "fact_id": "origin",
                }
                for row in rows
            ],
        )

    statements = 0

    def count_statement(*_args) -> None:
        nonlocal statements
        statements += 1

    event.listen(engine, "before_cursor_execute", count_statement)
    try:
        response = client.get(f"/api/v1/tasks/{task['id']}/graph")
    finally:
        event.remove(engine, "before_cursor_execute", count_statement)

    assert response.status_code == 200, response.text
    assert len(response.json()["intents"]) == 31
    assert statements <= 12


@pytest.mark.parametrize(
    "origin",
    [
        "https://example.com/private?token=secret",
        "微信小程序“测试商城”，AppID wx1234567890abcdef，重点测试投诉与退款流程",
        "Android App：测试客户端 3.2.1",
        "traffic-session.har",
    ],
)
def test_origin_accepts_nonempty_natural_language(client, origin: str) -> None:
    project = create_project(client)
    response = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "自然语言入口",
            "origin": origin,
            "goal": "测试目标",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["origin"] == {
        "host": None,
        "scheme": None,
        "port": None,
        "original_input": origin,
    }
    assert response.json()["scope"] == {"enabled": False, "targets": [], "exclude": []}


def test_blank_origin_does_not_leave_partial_task(client) -> None:
    project = create_project(client)
    response = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "空入口",
            "origin": "   ",
            "goal": "测试目标",
        },
    )
    assert response.status_code == 422
    assert client.get("/api/v1/tasks").json() == []


def test_archived_task_can_be_permanently_deleted(client) -> None:
    project = create_project(client)
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "任务",
            "origin": "example.com",
            "goal": "完成测试",
        },
    ).json()

    response = client.delete(f"/api/v1/projects/{project['id']}")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"

    assert client.post(f"/api/v1/tasks/{task['id']}/stop").status_code == 200
    delete_task = client.delete(f"/api/v1/tasks/{task['id']}")
    assert delete_task.status_code == 409
    assert "归档任务" in delete_task.json()["error"]["message"]
    assert client.post(f"/api/v1/tasks/{task['id']}/archive").status_code == 200
    assert client.delete(f"/api/v1/tasks/{task['id']}").status_code == 204
    assert client.get(f"/api/v1/tasks/{task['id']}").status_code == 404
    assert client.delete(f"/api/v1/projects/{project['id']}").status_code == 204


def test_permanent_task_delete_purges_database_history_and_files(
    client,
    engine,
    data_dir,
) -> None:
    project = create_project(client)
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "待永久删除任务",
            "origin": "微信小程序 wx1234567890abcdef",
            "goal": "验证删除完整性",
            "hints": ["删除后不保留任何任务记录"],
        },
    ).json()
    task_id = task["id"]

    artifact_file = data_dir / "tasks" / task_id / "artifacts" / "i001" / "findings.md"
    artifact_file.parent.mkdir(parents=True)
    artifact_file.write_text("temporary finding", encoding="utf-8")
    run_file = data_dir / "runs" / task_id / "i001" / "lease-test" / "session.jsonl"
    run_file.parent.mkdir(parents=True)
    run_file.write_text("{}\n", encoding="utf-8")

    assert client.post(f"/api/v1/tasks/{task_id}/stop").status_code == 200
    assert client.post(f"/api/v1/tasks/{task_id}/archive").status_code == 200
    response = client.delete(f"/api/v1/tasks/{task_id}")
    assert response.status_code == 204, response.text

    with engine.connect() as conn:
        assert conn.execute(
            select(func.count()).select_from(tasks).where(tasks.c.id == task_id)
        ).scalar_one() == 0
        for table in metadata.sorted_tables:
            if "task_id" not in table.c:
                continue
            remaining = conn.execute(
                select(func.count()).select_from(table).where(table.c.task_id == task_id)
            ).scalar_one()
            assert remaining == 0, f"{table.name} still contains rows for {task_id}"
        assert conn.execute(
            select(func.count()).select_from(id_counters).where(id_counters.c.scope == task_id)
        ).scalar_one() == 0

    assert not (data_dir / "tasks" / task_id).exists()
    assert not (data_dir / "runs" / task_id).exists()


def test_failed_permanent_delete_restores_database_and_files(
    client,
    data_dir,
    monkeypatch,
) -> None:
    project = create_project(client)
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "删除回滚任务",
            "origin": "rollback.example",
            "goal": "验证失败时完整回滚",
        },
    ).json()
    task_id = task["id"]
    artifact_file = data_dir / "tasks" / task_id / "artifacts" / "i001" / "findings.md"
    artifact_file.parent.mkdir(parents=True)
    artifact_file.write_text("must survive rollback", encoding="utf-8")

    assert client.post(f"/api/v1/tasks/{task_id}/stop").status_code == 200
    assert client.post(f"/api/v1/tasks/{task_id}/archive").status_code == 200

    repository = client.app.state.container.tasks.tasks

    def fail_delete(_conn, _task_id):
        raise RuntimeError("simulated database failure")

    monkeypatch.setattr(repository, "delete", fail_delete)
    with pytest.raises(RuntimeError, match="simulated database failure"):
        client.delete(f"/api/v1/tasks/{task_id}")

    assert client.get(f"/api/v1/tasks/{task_id}").status_code == 200
    assert artifact_file.read_text(encoding="utf-8") == "must survive rollback"


def test_scope_normalizes_exact_and_wildcard_targets(client) -> None:
    project = create_project(client)
    response = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "范围测试",
            "origin": "a.example.com",
            "goal": "测试授权边界",
            "scope": {
                "enabled": True,
                "targets": ["A.EXAMPLE.COM", "*.Example.com", "blocked.example.com"],
                "exclude": ["blocked.example.com"],
            },
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["scope"] == {
        "enabled": True,
        "targets": ["a.example.com", "*.example.com"],
        "exclude": ["blocked.example.com"],
    }


def test_task_settings_and_hints_can_be_edited_without_stale_graph_state(client) -> None:
    project = create_project(client)
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "原任务",
            "origin": "a.example.com",
        },
    ).json()
    assert task["goal"] == "对该系统进行渗透测试，并挖掘10个高置信度的漏洞"

    updated = client.patch(
        f"/api/v1/tasks/{task['id']}",
        json={
            "title": "新任务",
            "goal": "验证所有高价值攻击面",
            "scope": {"enabled": True, "targets": [], "exclude": ["blocked.example.com"]},
            "worker_pool": ["worker-b", "worker-b"],
            "guard_enabled": True,
            "external_report_enabled": True,
        },
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["title"] == "新任务"
    assert body["scope"] == {
        "enabled": True,
        "targets": [],
        "exclude": ["blocked.example.com"],
    }
    assert body["worker_pool"] == ["worker-b"]
    assert body["guard_enabled"] is True
    assert body["external_report_enabled"] is True

    graph = client.get(f"/api/v1/tasks/{task['id']}/graph").json()
    goal = next(item for item in graph["facts"] if item["id"] == "goal")
    assert goal["description"] == "验证所有高价值攻击面"
    assert graph["intents"][0]["worker_pool"] == ["worker-b"]

    hint = client.post(
        f"/api/v1/tasks/{task['id']}/hints",
        json={"content": "登录入口位于 /admin", "creator": "operator"},
    )
    assert hint.status_code == 201, hint.text
    changed = client.patch(
        f"/api/v1/tasks/{task['id']}/hints/{hint.json()['id']}",
        json={"content": "登录入口位于 /console"},
    )
    assert changed.status_code == 200
    assert changed.json()["content"] == "登录入口位于 /console"

    archived = client.patch(
        f"/api/v1/tasks/{task['id']}/hints/{hint.json()['id']}",
        json={"archived": True},
    )
    assert archived.status_code == 200
    assert client.get(f"/api/v1/tasks/{task['id']}/hints").json() == []


def test_task_archive_keeps_history_and_restore_does_not_resume(client) -> None:
    project = create_project(client, "归档测试")
    task = client.post(
        "/api/v1/tasks",
        json={"project_id": project["id"], "title": "保留历史", "origin": "archive.example.com"},
    ).json()

    assert client.post(f"/api/v1/tasks/{task['id']}/stop").status_code == 200
    archived = client.post(
        f"/api/v1/tasks/{task['id']}/archive",
        params={"actor": "test"},
    )
    assert archived.status_code == 200, archived.text
    assert archived.json()["archived"] is True
    assert archived.json()["status"] == "stopped"
    assert client.post(f"/api/v1/tasks/{task['id']}/resume").status_code == 409

    restored = client.post(
        f"/api/v1/tasks/{task['id']}/unarchive",
        params={"actor": "test"},
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["archived"] is False
    assert restored.json()["status"] == "stopped"


def test_running_task_cannot_be_archived(client) -> None:
    project = create_project(client, "运行中归档测试")
    task = client.post(
        "/api/v1/tasks",
        json={"project_id": project["id"], "title": "运行中", "origin": "running.example.com"},
    ).json()
    response = client.post(f"/api/v1/tasks/{task['id']}/archive")
    assert response.status_code == 409
    assert "先暂停" in response.json()["error"]["message"]


def test_archived_task_hides_normal_product_reads_until_restored(client) -> None:
    project = create_project(client, "归档披露测试")
    task = client.post(
        "/api/v1/tasks",
        json={"project_id": project["id"], "title": "隐藏产物", "origin": "hidden.example.com"},
    ).json()
    task_id = task["id"]
    assert client.post(f"/api/v1/tasks/{task_id}/stop").status_code == 200
    assert client.post(f"/api/v1/tasks/{task_id}/archive").status_code == 200

    assert client.get(f"/api/v1/tasks/{task_id}").json()["archived"] is True
    for path in (
        f"/api/v1/tasks/{task_id}/hints",
        f"/api/v1/tasks/{task_id}/graph",
        f"/api/v1/tasks/{task_id}/todos",
        f"/api/v1/tasks/{task_id}/vulnerabilities",
        f"/api/v1/tasks/{task_id}/artifacts",
        f"/api/v1/tasks/{task_id}/goal-cycles",
        f"/api/v1/tasks/{task_id}/goal-evaluations",
    ):
        assert client.get(path).status_code == 409, path

    assert client.post(f"/api/v1/tasks/{task_id}/unarchive").status_code == 200
    assert client.get(f"/api/v1/tasks/{task_id}/graph").status_code == 200
