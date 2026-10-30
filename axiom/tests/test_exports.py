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

    # 2. 没有图片时 auto 直出 md，不套 zip
    auto_res = client.get(f"/api/v1/tasks/{task_id}/vulnerabilities/v001/export")
    assert auto_res.status_code == 200
    assert auto_res.headers.get("content-type", "").startswith("text/markdown")
    assert f"{task_id}_v001_report.md" in auto_res.headers.get("content-disposition", "")
    assert "# 测试XSS漏洞" in auto_res.text

    # 3. 显式 zip 仍可用：没有图时就是一个只含 report.md 的包
    zip_res = client.get(f"/api/v1/tasks/{task_id}/vulnerabilities/v001/export?format=zip")
    assert zip_res.status_code == 200
    assert zip_res.headers.get("content-type") == "application/zip"
    assert f"{task_id}_v001_report.zip" in zip_res.headers.get("content-disposition", "")
    with zipfile.ZipFile(io.BytesIO(zip_res.content)) as zf:
        assert zf.namelist() == ["report.md"]
        assert "# 测试XSS漏洞" in zf.read("report.md").decode("utf-8")


def test_a_report_without_images_exports_a_plain_markdown_file(client) -> None:
    """No images → no archive.

    Explore's evidence/work files are no longer part of the deliverable; the report is
    produced from the stored reproduction, so the export is one file the recipient can
    open without unzipping.
    """
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
        for artifact_id, key, artifact_type in (
            ("a001", "artifacts/i001/findings.md", "explore.findings"),
            ("a002", "artifacts/i001/evidence/proof.txt", "explore.evidence"),
            ("a003", "artifacts/i001/work/probe_log.txt", "explore.work"),
        ):
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

    response = client.get(f"/api/v1/tasks/{task_id}/vulnerabilities/v001/export")

    assert response.status_code == 200
    assert response.headers.get("content-type", "").startswith("text/markdown")
    # explore 的 evidence/work 文件都不再进包
    assert "probe_log" not in response.text
    assert "proof.txt" not in response.text

    # 显式 zip 里也只有报告本身
    forced = client.get(f"/api/v1/tasks/{task_id}/vulnerabilities/v001/export?format=zip")
    with zipfile.ZipFile(io.BytesIO(forced.content)) as archive:
        assert archive.namelist() == ["report.md"]


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


def test_a_report_with_images_exports_markdown_plus_the_images(client) -> None:
    """Report包 = md + images，且 md 的引用路径与包内文件名一致。

    这是交付物的核心契约：md 里的 ![](images/x.png) 必须能在解压后的同一层找到 x.png，
    否则对方收到的是一份裂图报告。
    """
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

    response = client.get(f"/api/v1/tasks/{task_id}/vulnerabilities/v001/export")

    assert response.status_code == 200
    assert response.headers.get("content-type") == "application/zip"
    assert f"{task_id}_v001_report.zip" in response.headers.get("content-disposition", "")
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = archive.namelist()
        report = archive.read("report.md").decode("utf-8")
    assert "report.md" in names
    image_names = [name for name in names if name.startswith("images/")]
    # 文件名带步骤号与种类：交付目录里一眼看出哪张是报文、哪张是业务图
    assert [name for name in image_names] == ["images/step-02-business-shot.png"]
    # md 引用的路径必须与包内真实文件名逐字一致
    assert "![命令回显](images/step-02-business-shot.png)" in report
    # 图片紧跟在第 2 步的响应之后，而不是堆在文末
    assert report.index("### 步骤 2") < report.index("images/step-02-business-shot.png")
    # 报告正文里不再有独立的「证据」章节
    assert "## 证据" not in report


def test_a_result_report_exports_with_its_attack_path_and_no_score(client) -> None:
    """成果报告导出与页面同源：有攻击链、无分数。"""
    from axiom.modules.redteam.tables import red_team_chains, red_team_results
    from axiom.shared.clock import utcnow

    project = client.post("/api/v1/projects", json={"title": "成果导出"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "红队",
            "origin": "a.example.com",
            "goal": "取得 100 积分",
            "engagement_mode": "red_team",
            "red_team_objective": {
                "initial_principal": {"id": "user:member"},
                "target_capabilities": ["host:shell"],
                "target_assets": ["a.example.com"],
                "target_points": 100,
            },
        },
    ).json()
    container = client.app.state.container
    with container.engine.begin() as conn:
        conn.execute(
            red_team_chains.insert().values(
                id="ch001",
                task_id=task["id"],
                title="内网横向",
                initial_principal_json={"id": "user:member"},
                target_capabilities_json=[],
                target_assets_json=[],
                status="open",
                status_reason="",
                current_state_json={},
                source_fact_ids_json=[],
                source_intent_ids_json=[],
                created_by="human.operator",
                created_at=utcnow(),
                updated_at=utcnow(),
            )
        )
        conn.execute(
            red_team_results.insert().values(
                id="x001",
                task_id=task["id"],
                chain_id="ch001",
                intent_id="i001",
                result_type="pivot",
                title="横向到内网主机",
                description="拿到 192.168.0.95 的 shell",
                principal="user:member",
                tenant="",
                capabilities_before_json=[],
                capabilities_gained_json=["host:shell"],
                capabilities_after_json=[],
                target_asset="a.example.com",
                data_fields_json=[],
                data_summary="",
                evidence_refs_json=[],
                reproduction_json={
                    "entry_point": "GET /shell.php",
                    "prerequisites": ["已有 webshell"],
                    "chain_gain": "拿到 shell",
                    "steps": [
                        {
                            "title": "执行 id",
                            "request": "GET /shell.php?c=id HTTP/1.1\r\nHost: a.example.com\r\n\r\n",
                            "response": "HTTP/1.1 200 OK\r\n\r\nuid=0(root)",
                            "proves": "命令执行成立",
                        }
                    ],
                },
                status="verified",
                reported_by="worker-a",
                points_declared=30,
                points_awarded=35,
                review_reason="复现完整",
                created_at=utcnow(),
                updated_at=utcnow(),
            )
        )

    response = client.get(f"/api/v1/tasks/{task['id']}/red-team/results/x001/export")

    assert response.status_code == 200
    assert response.headers.get("content-type", "").startswith("text/markdown")
    assert f"{task['id']}_x001_report.md" in response.headers.get("content-disposition", "")
    body = response.text
    assert "### 步骤 1" in body
    assert "uid=0(root)" in body
    # 分数只属于裁决，不进报告正文
    assert "35" not in body
    assert "积分" not in body


def test_a_picture_already_named_for_its_step_is_not_prefixed_twice(client) -> None:
    """``step-02-shot.png`` attached to step 2 keeps its name.

    Otherwise the archive ends up with ``step-02-step-02-shot.png``: harmless to a
    machine, confusing to the person who attached it.
    """
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
        files={"file": ("step-02-shot.png", b"\x89PNG\r\n\x1a\n", "image/png")},
        data={"actor": "reviewer"},
    )
    client.post(
        f"/api/v1/tasks/{task_id}/vulnerabilities/v001/evidence",
        json={"artifact_id": upload.json()["id"], "step_index": 2},
    )

    response = client.get(f"/api/v1/tasks/{task_id}/vulnerabilities/v001/export")

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = archive.namelist()
        report = archive.read("report.md").decode("utf-8")
    assert "images/step-02-shot.png" in names
    assert "images/step-02-step-02-shot.png" not in names
    assert "](images/step-02-shot.png)" in report


def test_a_step_exports_its_packet_shot_before_its_business_shot(client) -> None:
    """同一步的两张图：报文图在前，业务图在后。

    ``kind`` 也进了文件名，所以在解压出来的目录里就能看出来哪张是回包、哪张是效果，
    不用打开报告逐条对。顺序由服务端的排序保证，不靠审核者的挂载顺序。
    """
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
    base = f"/api/v1/tasks/{task_id}/vulnerabilities/v001/evidence/capture"
    # 故意先挂业务图，验证顺序不是挂载顺序
    business = client.post(
        base,
        files={"file": ("admin.png", b"\x89PNG\r\n\x1a\nb", "image/png")},
        data={"step_index": "1", "kind": "business", "caption": "后台可见"},
    )
    packet = client.post(
        base,
        files={"file": ("resp.png", b"\x89PNG\r\n\x1a\np", "image/png")},
        data={"step_index": "1", "kind": "packet", "caption": "上传回包"},
    )
    assert business.status_code == 201, business.text
    assert packet.status_code == 201, packet.text

    response = client.get(f"/api/v1/tasks/{task_id}/vulnerabilities/v001/export")

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = [name for name in archive.namelist() if name.startswith("images/")]
        report = archive.read("report.md").decode("utf-8")
    assert names == [
        "images/step-01-packet-resp.png",
        "images/step-01-business-admin.png",
    ]
    assert report.index("step-01-packet-resp.png") < report.index("step-01-business-admin.png")


def test_a_two_packet_step_exports_both_packets_and_one_description(client) -> None:
    """一步两组报文：导出里两组都按顺序出现，描述只写一次。"""
    task_id = _task(client)
    client.post(
        f"/api/v1/tasks/{task_id}/vulnerabilities",
        json={
            "title": "覆盖他人客户导入文件",
            "type": "越权",
            "severity": "high",
            "description": "仅凭出库凭证即可覆盖他人客户文件",
            "prerequisites": "持有任意登录账号",
            "impact": "可篡改他人客户主数据",
            "reproduction": {
                "entry_point": "POST /admin/v1/file/temporary-credential",
                "prerequisites": ["持有任意登录账号"],
                "steps": [
                    {
                        "title": "申领凭证并覆盖文件",
                        "exchanges": [
                            {"request": "POST /admin/v1/file/temporary-credential HTTP/1.1\r\nHost: e.test\r\n\r\n{}",
                             "response": "HTTP/1.1 200 OK\r\n\r\n{\"code\":1000}"},
                            {"request": "POST https://oss.test/ HTTP/1.1\r\n\r\nkey=000/poc.xlsx",
                             "response": "HTTP/1.1 200 OK\r\n\r\n{\"ok\":true}"},
                        ],
                        "proves": "文件被覆盖到他人客户目录",
                    }
                ],
            },
            "source": "human",
            "reported_by": "tester",
        },
    )

    report = client.get(f"/api/v1/tasks/{task_id}/vulnerabilities/v001/export?format=markdown").text

    assert "### 步骤 1" in report
    assert report.count("描述：文件被覆盖到他人客户目录") == 1
    assert "报文 2：" in report
    assert report.index("temporary-credential") < report.index("oss.test")
    assert "## 利用前提" in report
    # 利用前提排在描述之后、测试过程之前
    assert report.index("## 描述") < report.index("## 利用前提") < report.index("## 测试过程")
