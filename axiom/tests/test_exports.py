from __future__ import annotations


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
