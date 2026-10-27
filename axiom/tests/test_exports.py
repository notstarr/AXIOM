from __future__ import annotations

import io
import zipfile


def _task(client):
    project = client.post("/api/v1/projects", json={"title": "导出项目"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={"project_id": project["id"], "title": "导出任务", "origin": "https://example.test"},
    ).json()
    return task["id"]


def test_task_overview_export_excludes_audit_events_and_includes_graph(client) -> None:
    task_id = _task(client)
    hint = client.post(f"/api/v1/tasks/{task_id}/hints", json={"content": "只测主站"})
    assert hint.status_code == 201

    response = client.get(f"/api/v1/tasks/{task_id}/export?format=yaml")
    assert response.status_code == 200, response.text
    assert "概况:" in response.text
    assert "facts:" in response.text
    assert "intents:" in response.text
    assert "只测主站" in response.text
    assert "audit_events" not in response.text


def test_task_timeline_and_vulnerability_exports_are_text(client) -> None:
    task_id = _task(client)
    timeline = client.get(f"/api/v1/tasks/{task_id}/export?format=timeline")
    assert timeline.status_code == 200
    assert "任务时间线" in timeline.text

    report = client.get(f"/api/v1/tasks/{task_id}/vulnerabilities-export")
    assert report.status_code == 200
    assert "当前没有漏洞记录" in report.text


def test_vulnerability_export_zip_and_markdown(client) -> None:
    import io
    import zipfile
    from axiom.modules.vulnerabilities.tables import vulnerabilities

    task_id = _task(client)
    container = client.app.state.container

    # 在数据库中直接插入一个测试漏洞
    with container.engine.begin() as conn:
        conn.execute(
            vulnerabilities.insert().values(
                id="v001",
                task_id=task_id,
                intent_id="i001",
                title="测试XSS漏洞",
                type="xss",
                severity="high",
                description="测试XSS描述",
                impact="测试影响",
                status="已验证",
                reported_by="system",
                source="explore",
                created_at="2026-08-26T00:00:00Z",
                updated_at="2026-08-26T00:00:00Z",
                archived=False,
            )
        )

    # 1. 测试 Markdown 导出
    md_res = client.get(f"/api/v1/tasks/{task_id}/vulnerabilities/v001/export?format=markdown")
    assert md_res.status_code == 200
    assert "# 测试XSS漏洞" in md_res.text
    assert "测试XSS描述" in md_res.text

    # 2. 测试 Zip 导出
    zip_res = client.get(f"/api/v1/tasks/{task_id}/vulnerabilities/v001/export?format=zip")
    assert zip_res.status_code == 200
    assert zip_res.headers.get("content-type") == "application/zip"
    assert f"{task_id}_v001_export.zip" in zip_res.headers.get("content-disposition", "")

    # 解压验证 zip 包内容
    with zipfile.ZipFile(io.BytesIO(zip_res.content)) as zf:
        namelist = zf.namelist()
        assert "vulnerability.md" in namelist
        assert "findings.md" in namelist
        vuln_content = zf.read("vulnerability.md").decode("utf-8")
        assert "# 测试XSS漏洞" in vuln_content
        assert "测试XSS描述" in vuln_content


def test_vulnerability_export_zip_excludes_work_records(client) -> None:
    import io
    import zipfile
    from axiom.modules.artifacts.tables import artifacts
    from axiom.modules.vulnerabilities.tables import vulnerabilities

    task_id = _task(client)
    container = client.app.state.container
    with container.engine.begin() as conn:
        conn.execute(
            vulnerabilities.insert().values(
                id="v001",
                task_id=task_id,
                intent_id="i001",
                title="测试XSS漏洞",
                type="xss",
                severity="high",
                description="测试XSS描述",
                impact="测试影响",
                status="已验证",
                reported_by="system",
                source="explore",
                created_at="2026-08-26T00:00:00Z",
                updated_at="2026-08-26T00:00:00Z",
                archived=False,
            )
        )
        rows = (
            ("a001", "artifacts/i001/findings.md", "explore.findings"),
            ("a002", "artifacts/i001/evidence/proof.txt", "explore.evidence"),
            ("a003", "artifacts/i001/work/probe_log.txt", "explore.work"),
        )
        for artifact_id, key, artifact_type in rows:
            storage_key = f"tasks/{task_id}/{key}"
            path = container.store.resolve(storage_key)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"# {artifact_type}\n", encoding="utf-8")
            conn.execute(
                artifacts.insert().values(
                    id=artifact_id,
                    task_id=task_id,
                    intent_id="i001",
                    review_run_id=None,
                    artifact_type=artifact_type,
                    storage_key=storage_key,
                    summary=artifact_type,
                    content_hash="0" * 64,
                    size=path.stat().st_size,
                    created_by="system",
                    created_at="2026-08-26T00:00:00Z",
                    archived=False,
                )
            )

    response = client.get(f"/api/v1/tasks/{task_id}/vulnerabilities/v001/export?format=zip")

    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        namelist = zf.namelist()
        report = zf.read("vulnerability.md").decode("utf-8")
    # 交付包只要有报告正文与原始 findings；explore 的 evidence/work 文件都不再入包。
    assert namelist == ["vulnerability.md", "findings.md"]
    assert not any("work/" in name for name in namelist)
    # This row predates structured reproduction, so it renders through the legacy
    # branch rather than the skeleton — old exports keep their prose.
    assert "## 历史格式内容（只读）" in report


REPRODUCTION = {
    "entry_point": "POST /upload.php",
    "prerequisites": ["无需认证"],
    "steps": [
        {
            "title": "上传 PHP 文件",
            "request": "POST /upload.php HTTP/1.1\r\nHost: a.example.com\r\n\r\n--X--",
            "response": "HTTP/1.1 200 OK\r\n\r\n{\"url\":\"/uploads/shell.php\"}",
            "proves": "上传接口不校验类型",
        },
        {
            "title": "访问落地的 webshell",
            "request": "GET /uploads/shell.php?c=id HTTP/1.1\r\nHost: a.example.com\r\n\r\n",
            "response": "HTTP/1.1 200 OK\r\n\r\nuid=33(www-data)",
            "proves": "落地的 PHP 被执行，取得命令执行",
        },
    ],
}


def test_a_report_with_reproduction_exports_the_skeleton_in_step_order(client) -> None:
    task_id = _task(client)
    created = client.post(
        f"/api/v1/tasks/{task_id}/vulnerabilities",
        json={
            "title": "任意文件上传导致 RCE",
            "type": "文件上传",
            "severity": "critical",
            "description": "上传 PHP 后可执行命令",
            "prerequisites": "无需认证",
            "impact": "服务器接管",
            "remediation": "校验扩展名并移出 web 根目录",
            "reproduction": REPRODUCTION,
            "source": "human",
            "reported_by": "tester",
        },
    )
    assert created.status_code == 201, created.text

    markdown = client.get(
        f"/api/v1/tasks/{task_id}/vulnerabilities/v001/export?format=markdown"
    ).text

    assert "## 测试过程" in markdown
    assert "入口点" in markdown and "POST /upload.php" in markdown
    assert markdown.index("### 步骤 1") < markdown.index("### 步骤 2")
    assert "uid=33(www-data)" in markdown
    assert "## 修复建议" in markdown
    # 报告正文不展示分数（红队侧同理），漏洞侧本来就没有分数概念。
    assert "积分" not in markdown


def test_the_zip_carries_reviewer_evidence_under_its_step_folder(client) -> None:
    task_id = _task(client)
    client.post(
        f"/api/v1/tasks/{task_id}/vulnerabilities",
        json={
            "title": "任意文件上传导致 RCE",
            "type": "文件上传",
            "severity": "critical",
            "description": "上传 PHP 后可执行命令",
            "prerequisites": "无需认证",
            "impact": "服务器接管",
            "reproduction": REPRODUCTION,
            "source": "human",
            "reported_by": "tester",
        },
    )
    upload = client.post(
        f"/api/v1/tasks/{task_id}/artifacts/upload",
        files={"file": ("shot.png", b"\x89PNG\r\n\x1a\n", "image/png")},
        data={"actor": "reviewer"},
    )
    artifact_id = upload.json()["id"]
    client.post(
        f"/api/v1/tasks/{task_id}/vulnerabilities/v001/evidence",
        json={"artifact_id": artifact_id, "step_index": 2, "caption": "命令回显"},
    )

    response = client.get(f"/api/v1/tasks/{task_id}/vulnerabilities/v001/export?format=zip")

    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        namelist = zf.namelist()
        report = zf.read("vulnerability.md").decode("utf-8")
    # 图片按步骤号归档，和报告里的位置一致。
    assert any(name.startswith("evidence/step-02/") for name in namelist)
    assert "## 测试过程" in report
