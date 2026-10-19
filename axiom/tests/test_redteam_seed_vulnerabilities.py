from __future__ import annotations

import json
from pathlib import Path

from axiom.modules.context.injection import ContextInjectionService
from axiom.modules.context.schemas import ContextRevision


def _project(client, title: str) -> dict:
    return client.post("/api/v1/projects", json={"title": title}).json()


def _task(client, project_id: str, title: str, **extra) -> dict:
    payload = {
        "project_id": project_id,
        "title": title,
        "origin": "seed.example.com",
    }
    payload.update(extra)
    response = client.post("/api/v1/tasks", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _vuln(client, task: dict, title: str = "种子漏洞") -> dict:
    response = client.post(
        f"/api/v1/tasks/{task['id']}/vulnerabilities",
        json={
            "title": title,
            "type": "authorization",
            "severity": "high",
            "description": "测试描述",
            "impact": "测试影响",
            "affected_target": "seed.example.com",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _mark(client, task: dict, vuln_id: str, value: bool = True) -> dict:
    response = client.post(
        f"/api/v1/tasks/{task['id']}/vulnerabilities/{vuln_id}/red-team-exploitable",
        json={"value": value, "actor": "hexin"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_red_team_exploitable_toggle_roundtrip_and_filter(client) -> None:
    project = _project(client, "标记测试")
    task = _task(client, project["id"], "漏洞标记")
    marked = _vuln(client, task, "可利用")
    plain = _vuln(client, task, "普通")

    body = _mark(client, task, marked["id"])
    assert body["red_team_exploitable"] is True
    assert body["red_team_exploitable_by"] == "hexin"
    assert body["red_team_exploitable_at"]

    response = client.get(
        f"/api/v1/vulnerabilities?task_id={task['id']}&red_team_exploitable=true&limit=10"
    )
    assert response.status_code == 200
    assert response.headers["X-Total-Count"] == "1"
    assert [item["id"] for item in response.json()] == [marked["id"]]

    cleared = _mark(client, task, marked["id"], value=False)
    assert cleared["red_team_exploitable"] is False
    assert cleared["red_team_exploitable_at"] is None
    assert [item["id"] for item in client.get(
        f"/api/v1/vulnerabilities?task_id={task['id']}&red_team_exploitable=true&limit=10"
    ).json()] == []
    assert plain["red_team_exploitable"] is False


def test_red_team_exploitable_survives_review_relevant_edit(client) -> None:
    project = _project(client, "标记保留")
    task = _task(client, project["id"], "字段编辑")
    vuln = _vuln(client, task)
    _mark(client, task, vuln["id"])

    patched = client.patch(
        f"/api/v1/tasks/{task['id']}/vulnerabilities/{vuln['id']}",
        json={"title": "改过标题的漏洞", "status": "待验证"},
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["title"] == "改过标题的漏洞"
    assert body["status"] == "待验证"
    assert body["red_team_exploitable"] is True


def test_create_task_seed_validation(client) -> None:
    project = _project(client, "种子校验")
    source = _task(client, project["id"], "漏洞源")
    redteam = _task(client, project["id"], "红队源", engagement_mode="red_team")

    missing = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "种子不存在",
            "origin": "seed.example.com",
            "engagement_mode": "red_team",
            "seed_vulnerability_task_id": "t_404",
        },
    )
    assert missing.status_code == 404

    wrong_mode = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "种子是红队",
            "origin": "seed.example.com",
            "engagement_mode": "red_team",
            "seed_vulnerability_task_id": redteam["id"],
        },
    )
    assert wrong_mode.status_code == 409

    wrong_engagement = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "漏洞模式带种子",
            "origin": "seed.example.com",
            "seed_vulnerability_task_id": source["id"],
        },
    )
    assert wrong_engagement.status_code == 422

    cross_project = _project(client, "另一个项目")
    created = client.post(
        "/api/v1/tasks",
        json={
            "project_id": cross_project["id"],
            "title": "跨项目种子",
            "origin": "seed.example.com",
            "engagement_mode": "red_team",
            "seed_vulnerability_task_id": source["id"],
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["seed_vulnerability_task_id"] == source["id"]


def _seed_revision(client, redteam: dict, phase: str, intent_id: str) -> dict:
    response = client.post(
        f"/api/v1/tasks/{redteam['id']}/context",
        json={"phase": phase, "intent_id": intent_id},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_seed_vulnerabilities_reach_redteam_bootstrap_only(client) -> None:
    project = _project(client, "种子注入")
    source = _task(client, project["id"], "漏洞源任务")
    marked = _vuln(client, source, "标记漏洞")
    _vuln(client, source, "未标记漏洞")
    _mark(client, source, marked["id"])

    redteam = _task(
        client,
        project["id"],
        "红队任务",
        engagement_mode="red_team",
        seed_vulnerability_task_id=source["id"],
    )
    revision = _seed_revision(client, redteam, "bootstrap", "i001")
    manifest = json.loads(Path(revision["manifest_path"]).read_text(encoding="utf-8"))
    assert "seed_vulnerabilities" in manifest["indexes"]
    payload = json.loads(
        (Path(revision["manifest_path"]).parent / "seed_vulnerabilities.json").read_text(encoding="utf-8")
    )
    assert payload["source_task_id"] == source["id"]
    assert payload["truncated"] is False
    assert [item["id"] for item in payload["vulnerabilities"]] == [marked["id"]]
    assert payload["vulnerabilities"][0]["severity"] == "high"

    model = ContextRevision.model_validate(revision)
    bootstrap = ContextInjectionService().build(
        revision=model, phase="bootstrap", intent_id="i001"
    )
    assert "seed_vulnerabilities" in bootstrap.text
    for phase, intent_id in (("explore", "i002"), ("reason", None), ("plan", None)):
        injected = ContextInjectionService().build(
            revision=model, phase=phase, intent_id=intent_id
        )
        assert "seed_vulnerabilities" not in injected.text


def test_seed_vulnerabilities_absent_without_marks_and_vulnerability_mode(client) -> None:
    project = _project(client, "零标记")
    source = _task(client, project["id"], "无标记源")
    _vuln(client, source)
    redteam = _task(
        client,
        project["id"],
        "红队零标记",
        engagement_mode="red_team",
        seed_vulnerability_task_id=source["id"],
    )
    revision = _seed_revision(client, redteam, "bootstrap", "i001")
    manifest = json.loads(Path(revision["manifest_path"]).read_text(encoding="utf-8"))
    assert "seed_vulnerabilities" not in manifest["indexes"]
    assert not (Path(revision["manifest_path"]).parent / "seed_vulnerabilities.json").exists()

    plain = _task(client, project["id"], "漏洞模式任务")
    revision = _seed_revision(client, plain, "bootstrap", "i001")
    manifest = json.loads(Path(revision["manifest_path"]).read_text(encoding="utf-8"))
    assert "seed_vulnerabilities" not in manifest["indexes"]


def test_seed_vulnerabilities_capped_at_fifty(client) -> None:
    project = _project(client, "截断")
    source = _task(client, project["id"], "大量标记")
    for index in range(51):
        vuln = _vuln(client, source, f"漏洞{index:02d}")
        _mark(client, source, vuln["id"])
    redteam = _task(
        client,
        project["id"],
        "红队截断",
        engagement_mode="red_team",
        seed_vulnerability_task_id=source["id"],
    )
    revision = _seed_revision(client, redteam, "bootstrap", "i001")
    payload = json.loads(
        (Path(revision["manifest_path"]).parent / "seed_vulnerabilities.json").read_text(encoding="utf-8")
    )
    assert payload["truncated"] is True
    assert len(payload["vulnerabilities"]) == 50
