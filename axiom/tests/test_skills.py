from __future__ import annotations

import json
import shutil

import yaml
from fastapi.testclient import TestClient

import axiom.modules.skills.service as skills_service_module
from axiom.app import create_app
from axiom.modules.skills.service import SkillService


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


def test_editing_bundled_skill_preserves_auxiliary_files_in_managed_override(client) -> None:
    original_files = client.get("/api/v1/skills/miniapp/files")
    assert original_files.status_code == 200, original_files.text
    original_paths = {item["path"] for item in original_files.json()}
    assert "scripts/miniapp_decompile_analyze.py" in original_paths
    assert "scripts/miniapp_regex_rules.json" in original_paths
    assert "profiles/kkv-minikkv.json" in original_paths

    original = client.get("/api/v1/skills/miniapp").json()
    saved = client.put(
        "/api/v1/skills/miniapp",
        json={"content": original["content"].replace("Reqable MCP", "MCP")},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["source"] == "managed"

    managed_files = client.get("/api/v1/skills/miniapp/files")
    assert managed_files.status_code == 200, managed_files.text
    managed_paths = {item["path"] for item in managed_files.json()}
    assert {
        "SKILL.md",
        "scripts/miniapp_decompile_analyze.py",
        "scripts/miniapp_regex_rules.json",
        "profiles/kkv-minikkv.json",
    } <= managed_paths
    assert not any("__pycache__" in path for path in managed_paths)

    script = client.get(
        "/api/v1/skills/miniapp/files/scripts/miniapp_decompile_analyze.py"
    )
    assert script.status_code == 200, script.text
    assert "DEFAULT_RULES" in script.json()["content"]


def test_deleting_managed_override_removes_materialized_support_files(
    client, data_dir
) -> None:
    original = client.get("/api/v1/skills/miniapp").json()
    saved = client.put(
        "/api/v1/skills/miniapp",
        json={"content": original["content"]},
    )
    assert saved.status_code == 200, saved.text
    managed = data_dir / "skills" / "miniapp"
    assert (managed / "scripts" / "miniapp_decompile_analyze.py").is_file()

    deleted = client.delete("/api/v1/skills/miniapp")
    assert deleted.status_code == 204, deleted.text
    assert not managed.exists()

    restored = client.get("/api/v1/skills/miniapp")
    assert restored.status_code == 200, restored.text
    assert restored.json()["source"] == "bundled"



def test_deleting_bundled_support_file_is_sticky_until_recreated(client, data_dir) -> None:
    original = client.get("/api/v1/skills/ffuf").json()
    saved = client.put("/api/v1/skills/ffuf", json={"content": original["content"]})
    assert saved.status_code == 200, saved.text
    relative_path = "references/Web-Fuzzing-Box/.gitignore"
    managed = data_dir / "skills" / "ffuf" / relative_path
    assert managed.is_file()

    deleted = client.delete(f"/api/v1/skills/ffuf/files/{relative_path}")
    assert deleted.status_code == 204, deleted.text
    assert not managed.exists()

    listed = client.get("/api/v1/skills/ffuf/files")
    assert listed.status_code == 200, listed.text
    assert relative_path not in {item["path"] for item in listed.json()}

    recreated = client.put(
        f"/api/v1/skills/ffuf/files/{relative_path}",
        json={"content": ".DS_Store\n"},
    )
    assert recreated.status_code == 200, recreated.text
    assert managed.read_text(encoding="utf-8") == ".DS_Store\n"
    assert relative_path in {item["path"] for item in client.get("/api/v1/skills/ffuf/files").json()}
    deletion_state = data_dir / "skills" / ".skill-file-deletions.json"
    assert not deletion_state.exists() or relative_path not in deletion_state.read_text(encoding="utf-8")


def test_skill_file_preview_is_bounded_and_reports_full_size(client) -> None:
    name = "large-preview"
    content = "A" * (512 * 1024 + 37)
    created = client.post(
        "/api/v1/skills",
        json={"name": name, "description": "large", "content": "# Large\n"},
    )
    assert created.status_code == 201, created.text
    saved = client.put(
        f"/api/v1/skills/{name}/files/references/large.txt",
        json={"content": content},
    )
    assert saved.status_code == 200, saved.text

    full = client.get(f"/api/v1/skills/{name}/files/references/large.txt")
    assert full.status_code == 200, full.text
    assert len(full.json()["content"]) == len(content) + 1
    assert full.json()["size"] == len(content) + 1
    assert full.json()["truncated"] is False

    preview = client.get(
        f"/api/v1/skills/{name}/files/references/large.txt?preview_bytes=524288"
    )
    assert preview.status_code == 200, preview.text
    payload = preview.json()
    assert len(payload["content"].encode("utf-8")) == 512 * 1024
    assert payload["size"] == len(content) + 1
    assert payload["truncated"] is True


def test_ffuf_bundled_references_include_complete_upstream_tree(client) -> None:
    files = client.get("/api/v1/skills/ffuf/files")
    assert files.status_code == 200, files.text
    paths = {item["path"] for item in files.json() if "__pycache__" not in item["path"]}
    assert len(paths) == 555  # 553 upstream files plus SKILL.md and the runner.
    assert not any("__pycache__" in path for path in paths)
    assert {
        "SKILL.md",
        "scripts/ffuf_background.sh",
        "references/Web-Fuzzing-Box/README.md",
        "references/Web-Fuzzing-Box/README_CN.md",
        "references/Web-Fuzzing-Box/Brute/Username/Top500_Username.txt",
        "references/Web-Fuzzing-Box/Dir/Others/Api.txt",
        "references/Web-Fuzzing-Box/Vuln/Sql_Injection/Sql_Params.txt",
        "references/Web-Fuzzing-Box/Web/Dict/Others/Top100_Parameters.txt",
        "references/Web-Fuzzing-Box/Other/2W_Words.txt",
    } <= paths
    assert not any(path.startswith("references/parameter.txt") for path in paths)
    assert "scripts/build_dictionaries.py" not in paths
    assert "references/fuzz.md" not in paths

    readme = client.get(
        "/api/v1/skills/ffuf/files/references/Web-Fuzzing-Box/README_CN.md"
    )
    assert readme.status_code == 200, readme.text
    assert "Web-Fuzzing-Box" in readme.json()["content"]


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


def test_execution_input_contains_skill_directories_and_injected_skill_context(client) -> None:
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
    assert "SKILL.md" in body["injected_system_context"]
    assert body["injected_context_hash"]


def test_bundled_skill_is_read_only_and_enabled_state_is_persisted(client) -> None:
    listed = client.get("/api/v1/skills").json()
    assert next(item for item in listed if item["name"] == "whatisvul")["enabled"] is True

    disabled = client.patch("/api/v1/skills/whatisvul/enabled", json={"enabled": False})
    assert disabled.status_code == 409, disabled.text
    listed_again = client.get("/api/v1/skills").json()
    assert next(item for item in listed_again if item["name"] == "whatisvul")["enabled"] is True

    forbidden = client.put(
        "/api/v1/skills/whatisvul/files/README.md",
        json={"content": "不应改写 bundled 文件"},
    )
    assert forbidden.status_code == 409, forbidden.text


def test_legacy_disabled_whatisvul_state_is_repaired_before_leases(
    engine, data_dir
) -> None:
    skills_dir = data_dir / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    (skills_dir / ".skill-state.json").write_text(
        json.dumps({"whatisvul": False, "browser-control": False}),
        encoding="utf-8",
    )

    with TestClient(create_app(engine=engine, data_dir=data_dir)) as test_client:
        skills = test_client.get("/api/v1/skills").json()
        whatisvul = next(item for item in skills if item["name"] == "whatisvul")
        browser = next(item for item in skills if item["name"] == "browser-control")
        assert whatisvul["enabled"] is True
        assert browser["enabled"] is False

        matrix = test_client.get("/api/v1/phase-capabilities").json()
        record = next(
            item
            for item in matrix["items"]
            if item["capability_kind"] == "skill"
            and item["capability_key"] == "whatisvul"
        )
        assert record["scopes"] == ["common"]

    state = json.loads((skills_dir / ".skill-state.json").read_text(encoding="utf-8"))
    assert state == {"browser-control": False, "whatisvul": True}



def test_ffuf_bundled_skill_contains_manual_and_background_runbook(client) -> None:
    listed = client.get("/api/v1/skills")
    assert listed.status_code == 200, listed.text
    summary = next(item for item in listed.json() if item["name"] == "ffuf")
    assert summary["source"] == "bundled"
    assert "FFUF" in summary["description"]

    detail = client.get("/api/v1/skills/ffuf")
    assert detail.status_code == 200, detail.text
    metadata, body = _split_skill_document(detail.json()["content"])
    assert metadata["name"] == "ffuf"
    assert "files" not in metadata["requirements"]
    assert metadata["requirements"]["commands"] == [
        {"name": "ffuf", "args": ["-V"], "contains": "ffuf version"}
    ]
    assert "后台运行" in body
    assert "ffuf_background.sh" in body
    assert "不要自行拼接 `nohup`、`&`" in body
    assert "nohup ffuf" not in body
    assert "-request request.txt" in body
    assert "Query 参数名或值" in body
    assert "Header、Cookie、Form 和 JSON Body" in body
    assert "不生成、扩充、下载或改写上游字典" in body
    assert "Reqable" not in body
    assert "常见问题排查" in body
    assert "Web-Fuzzing-Box" in body
    assert "README_CN.md" in body
    assert "FFUF_DICTIONARY_ROOT" in body
    assert "references/Web-Fuzzing-Box/README_CN.md" in body

    files = client.get("/api/v1/skills/ffuf/files")
    assert files.status_code == 200, files.text
    paths = {item["path"] for item in files.json() if "__pycache__" not in item["path"]}
    assert len(paths) == 555
    assert "references/Web-Fuzzing-Box/README_CN.md" in paths
    assert "references/fuzz.md" not in paths
    assert "scripts/build_dictionaries.py" not in paths


def test_managed_copy_that_was_synced_follows_bundled_skill_updates(data_dir, tmp_path) -> None:
    bundled_root = tmp_path / "bundled"
    bundled_skill = bundled_root / "auto-sync" / "SKILL.md"
    bundled_skill.parent.mkdir(parents=True)
    bundled_skill.write_text("---\nname: auto-sync\ndescription: auto\n---\n\n旧正文\n", encoding="utf-8")

    service = SkillService(data_dir)
    service.bundled_root = bundled_root.resolve()
    managed_skill = data_dir / "skills" / "auto-sync" / "SKILL.md"
    managed_skill.parent.mkdir(parents=True)
    shutil.copy2(bundled_skill, managed_skill)

    first = next(item for item in service.list() if item.name == "auto-sync")
    assert first.source == "managed"
    assert first.sync_status == "synced"

    bundled_skill.write_text("---\nname: auto-sync\ndescription: auto\n---\n\n新正文\n", encoding="utf-8")
    refreshed = next(item for item in service.list() if item.name == "auto-sync")
    assert refreshed.content.endswith("新正文\n")
    assert refreshed.sync_status == "synced"
    assert managed_skill.read_text(encoding="utf-8").endswith("新正文\n")


def test_synced_bundled_support_file_follows_bundled_updates(data_dir, tmp_path) -> None:
    bundled_root = tmp_path / "bundled"
    bundled_directory = bundled_root / "auto-sync"
    bundled_directory.mkdir(parents=True)
    (bundled_directory / "SKILL.md").write_text(
        "---\nname: auto-sync\ndescription: auto\n---\n\n正文\n",
        encoding="utf-8",
    )
    bundled_reference = bundled_directory / "references" / "guide.md"
    bundled_reference.parent.mkdir(parents=True)
    bundled_reference.write_text("旧附件\n", encoding="utf-8")

    service = SkillService(data_dir)
    service.bundled_root = bundled_root.resolve()
    managed_directory = data_dir / "skills" / "auto-sync"
    managed_directory.mkdir(parents=True)
    shutil.copy2(bundled_directory / "SKILL.md", managed_directory / "SKILL.md")
    managed_reference = managed_directory / "references" / "guide.md"
    managed_reference.parent.mkdir(parents=True)
    shutil.copy2(bundled_reference, managed_reference)

    first = next(item for item in service.list() if item.name == "auto-sync")
    assert first.sync_status == "synced"

    bundled_reference.write_text("新附件\n", encoding="utf-8")
    refreshed = next(item for item in service.list() if item.name == "auto-sync")
    assert refreshed.sync_status == "synced"
    assert managed_reference.read_text(encoding="utf-8") == "新附件\n"


def test_skill_list_reuses_package_hash_until_files_change(
    data_dir, tmp_path, monkeypatch
) -> None:
    bundled_root = tmp_path / "bundled"
    bundled_directory = bundled_root / "large-package"
    bundled_directory.mkdir(parents=True)
    (bundled_directory / "SKILL.md").write_text(
        "---\nname: large-package\ndescription: large\n---\n\n正文\n",
        encoding="utf-8",
    )
    support = bundled_directory / "references" / "dictionary.txt"
    support.parent.mkdir(parents=True)
    support.write_text("版本一\n", encoding="utf-8")

    service = SkillService(data_dir)
    service.bundled_root = bundled_root.resolve()
    calls: list[str] = []
    original_tree_hash = skills_service_module._tree_hash

    def counting_tree_hash(root):
        calls.append(str(root))
        return original_tree_hash(root)

    monkeypatch.setattr(skills_service_module, "_tree_hash", counting_tree_hash)

    first = service.list()
    first_calls = len(calls)
    second = service.list()

    assert first_calls == 1
    assert len(calls) == first_calls
    assert second[0].effective_hash == first[0].effective_hash

    support.write_text("版本二\n", encoding="utf-8")
    refreshed = service.list()
    assert len(calls) == first_calls + 1
    assert refreshed[0].effective_hash != first[0].effective_hash


def test_custom_support_file_is_preserved_when_bundled_copy_changes(data_dir, tmp_path) -> None:
    bundled_root = tmp_path / "bundled"
    bundled_directory = bundled_root / "custom-support"
    bundled_directory.mkdir(parents=True)
    (bundled_directory / "SKILL.md").write_text(
        "---\nname: custom-support\ndescription: auto\n---\n\n正文\n",
        encoding="utf-8",
    )
    bundled_reference = bundled_directory / "references" / "guide.md"
    bundled_reference.parent.mkdir(parents=True)
    bundled_reference.write_text("内置附件一\n", encoding="utf-8")

    service = SkillService(data_dir)
    service.bundled_root = bundled_root.resolve()
    managed_directory = data_dir / "skills" / "custom-support"
    managed_directory.mkdir(parents=True)
    shutil.copy2(bundled_directory / "SKILL.md", managed_directory / "SKILL.md")
    managed_reference = managed_directory / "references" / "guide.md"
    managed_reference.parent.mkdir(parents=True)
    shutil.copy2(bundled_reference, managed_reference)

    first = next(item for item in service.list() if item.name == "custom-support")
    assert first.sync_status == "synced"
    service.save_file("custom-support", "references/guide.md", "用户定制附件")

    bundled_reference.write_text("内置附件二\n", encoding="utf-8")
    refreshed = next(item for item in service.list() if item.name == "custom-support")
    assert refreshed.sync_status == "bundled_update_available"
    assert refreshed.bundled_update_available is True
    assert managed_reference.read_text(encoding="utf-8") == "用户定制附件\n"


def test_custom_managed_skill_is_not_overwritten_and_reports_bundled_update(
    data_dir, tmp_path
) -> None:
    bundled_root = tmp_path / "bundled"
    bundled_skill = bundled_root / "custom-skill" / "SKILL.md"
    bundled_skill.parent.mkdir(parents=True)
    bundled_skill.write_text("---\nname: custom-skill\ndescription: bundled\n---\n\n版本一\n", encoding="utf-8")

    service = SkillService(data_dir)
    service.bundled_root = bundled_root.resolve()
    managed_skill = data_dir / "skills" / "custom-skill" / "SKILL.md"
    managed_skill.parent.mkdir(parents=True)
    managed_skill.write_text("---\nname: custom-skill\ndescription: custom\n---\n\n定制正文\n", encoding="utf-8")

    first = next(item for item in service.list() if item.name == "custom-skill")
    assert first.sync_status == "custom"
    assert first.bundled_update_available is False

    bundled_skill.write_text("---\nname: custom-skill\ndescription: bundled\n---\n\n版本二\n", encoding="utf-8")
    refreshed = next(item for item in service.list() if item.name == "custom-skill")
    assert refreshed.content.endswith("定制正文\n")
    assert refreshed.sync_status == "bundled_update_available"
    assert refreshed.bundled_update_available is True


def test_restore_bundled_skill_discards_explicit_override(client) -> None:
    original = client.get("/api/v1/skills/browser-control").json()
    saved = client.put(
        "/api/v1/skills/browser-control",
        json={"content": original["content"] + "\n# 定制修改\n"},
    )
    assert saved.status_code == 200, saved.text
    response = client.post("/api/v1/skills/browser-control/restore-bundled")
    assert response.status_code == 200, response.text
    assert response.json()["source"] == "bundled"
    assert "定制修改" not in response.json()["content"]
