from __future__ import annotations

import json
from pathlib import Path


def test_project_knowledge_is_editable_and_progressively_disclosed(client) -> None:
    project = client.post("/api/v1/projects", json={"title": "知识项目"}).json()
    created = client.post(
        f"/api/v1/projects/{project['id']}/knowledge",
        json={
            "title": "登录流程",
            "content": "管理员必须先访问统一认证站点，再携带会话返回目标系统。",
            "category": "authentication",
        },
    )
    assert created.status_code == 201, created.text
    knowledge = created.json()
    assert knowledge["id"] == "k001"

    updated = client.patch(
        f"/api/v1/projects/{project['id']}/knowledge/{knowledge['id']}",
        json={"title": "统一登录流程"},
    )
    assert updated.status_code == 200
    assert updated.json()["title"] == "统一登录流程"

    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "知识上下文",
            "origin": "a.example.com",
        },
    ).json()
    context = client.post(
        f"/api/v1/tasks/{task['id']}/context",
        json={"phase": "reason", "intent_id": "i001"},
    )
    assert context.status_code == 200, context.text
    manifest_path = Path(context.json()["manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    knowledge_index = json.loads(
        (manifest_path.parent / Path(manifest["indexes"]["knowledge"]).name).read_text(
            encoding="utf-8"
        )
    )
    assert knowledge_index[0]["title"] == "统一登录流程"
    assert "管理员必须先访问统一认证站点" not in manifest_path.read_text(encoding="utf-8")

    expanded = client.post(
        f"/api/v1/tasks/{task['id']}/context/expand",
        json={"refs": [f"knowledge:{knowledge['id']}"]},
    )
    assert expanded.status_code == 200
    assert expanded.json()["entries"][f"knowledge:{knowledge['id']}"]["content"].startswith(
        "管理员必须"
    )

    archived = client.patch(
        f"/api/v1/projects/{project['id']}/knowledge/{knowledge['id']}",
        json={"archived": True},
    )
    assert archived.status_code == 200
    assert client.get(f"/api/v1/projects/{project['id']}/knowledge").json() == []
