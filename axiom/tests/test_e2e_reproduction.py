"""End-to-end: explore reports via CLI-shaped payload, then report/export render it."""
import json
from pathlib import Path

def test_e2e_explore_cli_to_rendered_report(client):
    # 1) create project + vulnerability task
    project = client.post("/api/v1/projects", json={"title": "E2E"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={"project_id": project["id"], "title": "任务", "origin": "a.example.com", "goal": "目标"},
    ).json()

    # 2) explore submits the reproduction exactly as the CLI posts it
    repro = {
        "entry_point": "POST /upload.php",
        "prerequisites": ["无需认证"],
        "steps": [
            {
                "title": "上传 PHP",
                "request": "POST /upload.php HTTP/1.1\r\nHost: a.example.com\r\n\r\n--X--",
                "response": "HTTP/1.1 200 OK\r\n\r\n{\"url\":\"/uploads/shell.php\"}",
                "proves": "上传不校验类型",
            }
        ],
    }
    created = client.post(
        f"/api/v1/tasks/{task['id']}/vulnerabilities",
        json={
            "title": "任意文件上传",
            "type": "文件上传",
            "severity": "critical",
            "description": "上传 PHP 后可执行",
            "prerequisites": "无需认证",
            "impact": "服务器接管",
            "reproduction": repro,
            "source": "explore",
            "reported_by": "worker-a",
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["format_version"] == 3
    assert body["reproduction"]["steps"][0]["proves"] == "上传不校验类型"

    # 3) the rendered report
    report = client.get(f"/api/v1/tasks/{task['id']}/vulnerabilities/v001/report").json()
    assert report["subject_type"] == "vulnerability"
    process = next(s for s in report["sections"] if s["key"] == "process")
    steps = next(b for b in process["blocks"] if b["type"] == "steps")
    assert steps["steps"][0]["index"] == 1
    assert "POST /upload.php" in steps["steps"][0]["request"]

    # 4) markdown export round-trips the packet verbatim
    md = client.get(f"/api/v1/tasks/{task['id']}/vulnerabilities/v001/export?format=markdown").text
    assert "### 步骤 1" in md
    assert "POST /upload.php HTTP/1.1" in md

    # 5) the raw stored row (what the reviewer sees) also carries the reproduction
    detail = client.get(f"/api/v1/tasks/{task['id']}/vulnerabilities/v001").json()
    assert detail["reproduction"]["entry_point"] == "POST /upload.php"


def test_e2e_redteam_result_reports_with_a_reproduction_and_no_evidence_file(client, engine) -> None:
    """The red-team path: no --evidence-path, the packets are the proof."""
    from sqlalchemy import insert, update

    from axiom.modules.redteam.tables import red_team_chains, red_team_results
    from axiom.modules.graph.tables import intents
    from axiom.shared.clock import utcnow

    project = client.post("/api/v1/projects", json={"title": "E2E 红队"}).json()
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
    assert task["engagement_mode"] == "red_team"

    # Seed a verified Result that the new one will chain off, so the assembled
    # attack path has something to show.
    with engine.begin() as conn:
        conn.execute(
            insert(red_team_chains).values(
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
            insert(intents).values(
                id="i002",
                task_id=task["id"],
                chain_id="ch001",
                kind="explore",
                description="目标：SSRF 打云元数据。判据：拿到 STS",
                status="completed",
                creator="reason",
                created_at="2026-10-01T00:00:00Z",
            )
        )
        conn.execute(
            insert(red_team_results).values(
                id="x001",
                task_id=task["id"],
                chain_id="ch001",
                intent_id="i002",
                result_type="credential",
                title="取得 STS",
                description="",
                principal="user:member",
                tenant="",
                capabilities_before_json=[],
                capabilities_gained_json=[],
                capabilities_after_json=[],
                target_asset="",
                data_fields_json=[],
                data_summary="",
                evidence_refs_json=[],
                reproduction_json={"chain_gain": "拿到 STS 凭证"},
                status="verified",
                reported_by="worker-a",
                points_declared=5,
                points_awarded=5,
                review_reason="",
                created_at=utcnow(),
                updated_at=utcnow(),
            )
        )
        conn.execute(
            insert(intents).values(
                id="i003",
                task_id=task["id"],
                chain_id="ch001",
                kind="explore",
                description="目标：横向到 192.168.0.95。判据：能执行命令",
                status="completed",
                creator="reason",
                created_at="2026-10-02T00:00:00Z",
            )
        )

    listed = client.get(f"/api/v1/tasks/{task['id']}/red-team/results").json()
    assert [item["id"] for item in listed] == ["x001"]
    # 报告不再携带攻击链：整条路径属于图谱与血缘视图，不属于单条成果的报告
    assert "attack_path" not in listed[0]
    # 但逐步的证据仍然是这份报告的一部分
    assert listed[0]["evidence"] == []
