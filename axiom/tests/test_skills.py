from __future__ import annotations

import yaml


def _split_skill_document(content: str) -> tuple[dict, str]:
    lines = content.splitlines()
    assert lines[0] == "---"
    end = lines.index("---", 1)
    return yaml.safe_load("\n".join(lines[1:end])), "\n".join(lines[end + 1 :]).strip()


def test_skills_are_shared_paths_and_not_copied_per_worker(client) -> None:
    listed = client.get("/api/v1/skills")
    assert listed.status_code == 200, listed.text
    names = {item["name"] for item in listed.json()}
    assert "whatisvul" in names

    directories = client.get("/api/v1/skills/directories").json()
    assert len(directories) == 2
    assert all("skills" in path for path in directories)

    original = client.get("/api/v1/skills/whatisvul").json()
    assert "漏洞" in original["content"]
    assert original["source"] == "bundled"

    saved = client.put(
        "/api/v1/skills/whatisvul",
        json={"content": "# 自定义漏洞标准\n\n先验证影响。"},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["source"] == "managed"
    assert saved.json()["content"].startswith("# 自定义漏洞标准")

    listed_again = client.get("/api/v1/skills").json()
    selected = next(item for item in listed_again if item["name"] == "whatisvul")
    assert selected["source"] == "managed"
    assert "worker" not in selected["path"].lower()


def test_skill_save_keeps_one_frontmatter_layer_and_preserves_metadata(client) -> None:
    original = client.get("/api/v1/skills/browser-control").json()
    original_metadata, _ = _split_skill_document(original["content"])
    duplicated_payload = (
        "---\n"
        "name: browser-control\n"
        "description: 旧描述\n"
        "---\n\n"
        "---\n"
        "name: browser-control\n"
        "description: 更旧描述\n"
        "---\n\n"
        "# 自定义浏览器流程\n"
    )

    saved = client.put(
        "/api/v1/skills/browser-control",
        json={"description": "新描述", "content": duplicated_payload},
    )

    assert saved.status_code == 200, saved.text
    metadata, body = _split_skill_document(saved.json()["content"])
    assert metadata["name"] == "browser-control"
    assert metadata["description"] == "新描述"
    assert metadata["requirements"] == original_metadata["requirements"]
    assert body == "# 自定义浏览器流程"
    assert not body.startswith("---")


def test_execution_input_contains_skill_directories_without_skill_content(client) -> None:
    project = client.post("/api/v1/projects", json={"title": "Skill 注入"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={"project_id": project["id"], "title": "Skill", "origin": "a.example.com"},
    ).json()
    worker = client.put(
        "/api/v1/workers/worker-a",
        json={"capabilities": ["bootstrap"], "max_running": 1},
    )
    assert worker.status_code == 200
    claim = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/intents/i001/claim",
        json={"worker_name": "worker-a"},
    ).json()
    base = f"/api/v1/dispatch/tasks/{task['id']}/intents/i001"
    assert client.post(f"{base}/start", json={"lease_id": claim["lease_id"]}).status_code == 200
    execution = client.post(f"{base}/input", json={"lease_id": claim["lease_id"]})
    assert execution.status_code == 200, execution.text
    body = execution.json()
    assert body["skill_directories"]
    assert "SKILL.md" not in execution.text


def test_bundled_skill_is_read_only_and_enabled_state_is_persisted(client) -> None:
    listed = client.get("/api/v1/skills").json()
    assert next(item for item in listed if item["name"] == "whatisvul")["enabled"] is True

    disabled = client.patch("/api/v1/skills/whatisvul/enabled", json={"enabled": False})
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["enabled"] is False
    listed_again = client.get("/api/v1/skills").json()
    assert next(item for item in listed_again if item["name"] == "whatisvul")["enabled"] is False

    forbidden = client.put(
        "/api/v1/skills/whatisvul/files/README.md",
        json={"content": "不应改写 bundled 文件"},
    )
    assert forbidden.status_code == 409, forbidden.text
