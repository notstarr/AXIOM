"""审核者能自己改写描述类内容，但动不了定档字段。

背景：成果的描述、前提、复现、影响、修复建议全由 explore 上报时写死，审核者
只能挂截图 + 给裁决 + 核定分。explore 写得糙时（缺前提、步骤文字有误、影响面
判断偏差），审核者没有改的余地，只能在裁决理由里吐槽两句，而报告正文渲染的
仍是上报方原文。

这些用例锁住新行为：

* 审核者在自己的审核期里改描述，**不会**把成果打回待审、不会重新排队审核——
  那条语义属于「人工从外面改了内容」，对审核者自己就是自审循环；
* 漏洞侧改完描述后，本次审核的输入指纹要跟着刷新，否则下一步提交裁决会被
  「审核期间对象变了」挡死；
* 复现的**步骤数与每步报文数不可增删**：报文截图闸门正是按这个数量算要求，
  删一步就等于放松一份证据要求；
* 定档字段（标题、等级、类型、资产、数据条数、分值）改不了；
* 红队成果没有「前提/影响/修复建议」三列，传了要被拒；
* 就地覆盖意味着旧文会丢，所以必须留一笔 audit 事件。

**为什么挤在一个文件里而不是每个场景一个文件**：本仓库的测试库由所有用例共享，
每个用例前后都会 TRUNCATE 全部表（见 ``tests/conftest.py``）。相邻用例的
teardown 与下一个用例的 setup 会互相抢同一个库，数据在用例执行途中被清空是
已知现象。共用一条 task 的用例只经历一次 TRUNCATE，断言不受影响。
"""

from __future__ import annotations

import json

from sqlalchemy import select, update

from axiom.modules.audit.tables import audit_events
from axiom.modules.redteam.tables import red_team_chains, red_team_results
from axiom.modules.vulnerabilities.tables import vulnerabilities
from axiom.modules.vulnerability_reviews.tables import vulnerability_review_runs
from axiom.shared.clock import utcnow


def _reproduction(*, steps: int = 1, exchanges: int = 1, title: str = "改 username=admin 落库") -> dict:
    """构造一份结构化复现：steps 步，每步 exchanges 个报文。"""
    return {
        "entry_point": "POST /admin/index/info.html",
        "prerequisites": ["持有过期 JWT"],
        "chain_input": "已持有过期 JWT",
        "chain_gain": "取得 TERP 后台管理员会话",
        "steps": [
            {
                "title": title,
                "exchanges": [
                    {
                        "request": (
                            "POST /admin/index/info.html HTTP/1.1\r\n"
                            "Host: terp.t.kkguan.com\r\n\r\n"
                            f'{{"id":14473,"n":{index}}}'
                        ),
                        "response": 'HTTP/1.1 200 OK\r\n\r\n{"code":1}',
                    }
                    for index in range(exchanges)
                ],
                "proves": "提权由 username 字段决定",
            }
            for _ in range(steps)
        ],
    }


def _redteam_task(client) -> dict:
    project = client.post("/api/v1/projects", json={"title": "审核者改写"}).json()
    worker = client.put(
        "/api/v1/workers/worker-a",
        json={
            "capabilities": ["review"],
            "max_running": 4,
            "config": {"model": "review-model"},
        },
    )
    assert worker.status_code == 200, worker.text
    return client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "红队",
            "origin": "terp.t.kkguan.com",
            "goal": "取得 100 积分",
            "engagement_mode": "red_team",
            "red_team_objective": {
                "initial_principal": {"id": "user:sso"},
                "target_capabilities": ["app:admin"],
                "target_assets": ["terp.t.kkguan.com"],
                "target_points": 100,
            },
        },
    ).json()


def _vuln_task(client) -> dict:
    project = client.post("/api/v1/projects", json={"title": "审核者改写漏洞"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "漏洞",
            "origin": "review.example.com",
            "goal": "验证漏洞审核链路",
        },
    ).json()
    worker = client.put(
        "/api/v1/workers/reviewer-a",
        json={
            "capabilities": ["review"],
            "max_running": 4,
            "config": {"model": "review-model"},
        },
    )
    assert worker.status_code == 200, worker.text
    return task


def _seed_result(engine, task_id: str, result_id: str, *, reproduction: dict | None = None) -> None:
    with engine.begin() as conn:
        conn.execute(
            red_team_chains.insert().values(
                id="ch001",
                task_id=task_id,
                title="ERP 订单结算",
                initial_principal_json={"id": "user:sso"},
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
                id=result_id,
                task_id=task_id,
                chain_id="ch001",
                intent_id="i001",
                result_type="permission",
                title="生产 TERP 后台管理员会话",
                description="上报方写的描述",
                principal="user:sso",
                tenant="",
                capabilities_before_json=[],
                capabilities_gained_json=["app:admin"],
                capabilities_after_json=[],
                target_asset="terp.t.kkguan.com",
                data_fields_json=[],
                data_summary="",
                evidence_refs_json=[],
                reproduction_json=reproduction if reproduction is not None else _reproduction(),
                status="reviewing",
                reported_by="worker-a",
                points_declared=10,
                points_awarded=0,
                review_reason="",
                created_at=utcnow(),
                updated_at=utcnow(),
            )
        )


def _seed_vulnerability(client, task_id: str, *, reproduction: dict | None = None) -> dict:
    response = client.post(
        f"/api/v1/tasks/{task_id}/vulnerabilities",
        json={
            "title": "对象级授权缺失",
            "type": "越权访问",
            "severity": "high",
            "description": "上报方写的描述",
            "prerequisites": "需要任意低权限账号。",
            "impact": "造成跨用户数据泄露。",
            "remediation": "服务端校验对象归属。",
            "affected_target": "https://review.example.com/api/orders/2",
            "source": "human",
            "reported_by": "tester",
            "reproduction": reproduction if reproduction is not None else _reproduction(),
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _open_redteam_review(client, task_id: str, result_id: str) -> tuple[str, dict]:
    triggered = client.post(
        f"/api/v1/tasks/{task_id}/red-team/results/{result_id}/reviews",
        json={"actor": "human.operator", "reason": "复核"},
    )
    assert triggered.status_code == 201, triggered.text
    run_id = triggered.json()["id"]
    base = f"/api/v1/dispatch/tasks/{task_id}/vulnerability-review-runs/{run_id}"
    claimed = client.post(f"{base}/claim", json={"worker_name": "worker-a"})
    assert claimed.status_code == 200, claimed.text
    lease = {"lease_id": claimed.json()["lease_id"]}
    assert client.post(f"{base}/start", json=lease).status_code == 200
    return base, lease


def _open_vuln_review(client, task_id: str, vulnerability_id: str) -> tuple[str, dict]:
    runs = client.get(
        f"/api/v1/tasks/{task_id}/vulnerabilities/{vulnerability_id}/reviews"
    ).json()
    assert len(runs) == 1, runs
    run_id = runs[0]["id"]
    base = f"/api/v1/dispatch/tasks/{task_id}/vulnerability-review-runs/{run_id}"
    claimed = client.post(f"{base}/claim", json={"worker_name": "reviewer-a"})
    assert claimed.status_code == 200, claimed.text
    lease = {"lease_id": claimed.json()["lease_id"]}
    assert client.post(f"{base}/start", json=lease).status_code == 200
    return base, lease


def _attach_shots(client, task_id: str, *, result_id: str | None = None, vulnerability_id: str | None = None) -> None:
    """按步骤数挂足报文图，否则裁决会被闸门挡下。"""
    target = (
        f"/api/v1/tasks/{task_id}/red-team/results/{result_id}/evidence/capture"
        if result_id
        else f"/api/v1/tasks/{task_id}/vulnerabilities/{vulnerability_id}/evidence/capture"
    )
    response = client.post(
        target,
        files={"file": ("step-01.png", b"\x89PNG\r\n\x1a\n", "image/png")},
        data={"step_index": "1", "kind": "packet", "caption": "步骤 1 回包"},
    )
    assert response.status_code == 201, response.text


def test_reviewer_cli_rewrite_reaches_the_service_end_to_end(
    client,
    engine,
    monkeypatch,
    tmp_path,
) -> None:
    """CLI → HTTP → service → 库，一条路走通。

    前面的用例要么直接打 API、要么只看 CLI 拼出来的载荷；这条把两者接上：
    CLI 发的请求体由真实的服务端解析，落库后描述确实变了、状态确实没被复位。
    """
    from click.testing import CliRunner

    from axiom.cli import main

    task = _redteam_task(client)
    _seed_result(engine, task["id"], "x001")
    base, lease = _open_redteam_review(client, task["id"], "x001")
    run_id = base.rsplit("/", 1)[-1]

    def forward(method: str, path: str, payload: dict):
        # CLI 打的是 /api/v1 前缀下的路径，TestClient 直接用应用路由
        response = client.post(f"/api/v1{path}", json=payload)
        assert response.status_code == 200, response.text
        return response.json()

    monkeypatch.setattr("axiom.worker_cli._request", forward)

    reproduction = tmp_path / "reproduction.json"
    revised = _reproduction(title="审核者改写后的步骤标题")
    revised["steps"][0]["exchanges"][0]["response"] = 'HTTP/1.1 200 OK\r\n\r\n{"code":1,"by":"reviewer"}'
    reproduction.write_text(json.dumps(revised, ensure_ascii=False), encoding="utf-8")
    description = tmp_path / "description.md"
    description.write_text("审核者改写后的描述。", encoding="utf-8")

    result = CliRunner().invoke(
        main,
        [
            "review",
            "description",
            "submit",
            "--description-file",
            str(description),
            "--reproduction-file",
            str(reproduction),
        ],
        env={
            "AXIOM_SERVER": "http://127.0.0.1:8899",
            "AXIOM_TASK_ID": task["id"],
            "AXIOM_LEASE_ID": lease["lease_id"],
            "AXIOM_REVIEW_RUN_ID": run_id,
            "AXIOM_VULNERABILITY_ID": "",
        },
    )
    assert result.exit_code == 0, result.output

    with engine.connect() as conn:
        row = conn.execute(
            select(red_team_results).where(red_team_results.c.id == "x001")
        ).mappings().one()
        assert row["description"] == "审核者改写后的描述。"
        assert row["status"] == "reviewing"


def test_reviewer_rewrites_description_without_requeueing_and_cannot_touch_frozen_fields(
    client,
    engine,
) -> None:
    """红队侧：改描述不重置状态、不重新排队；定档字段与步骤结构改不动。"""
    task = _redteam_task(client)
    _seed_result(engine, task["id"], "x001")
    base, lease = _open_redteam_review(client, task["id"], "x001")

    # 改描述 + 改复现的文本（步骤数与报文数不变）
    revised = _reproduction(title="审核者改写后的步骤标题")
    revised["chain_gain"] = "取得生产 TERP 后台管理员会话（已复核）"
    revised["steps"][0]["exchanges"][0]["response"] = 'HTTP/1.1 200 OK\r\n\r\n{"code":1,"by":"reviewer"}'
    response = client.post(
        f"{base}/description",
        json={
            **lease,
            "description": "审核者改写后的描述。",
            "reproduction": revised,
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["applied_fields"] == ["description", "reproduction_json"]
    assert payload["review_subject_type"] == "redteam_result"
    assert payload["red_team_result_id"] == "x001"
    # 闸门要求要随改写后的复现回带给审核者，而不是等提交裁决时才被动告知
    assert payload["missing_packet_shots"] == [
        {"step": 1, "required": 1, "attached": 0}
    ]
    assert payload["ready"] is False

    with engine.connect() as conn:
        row = conn.execute(
            select(red_team_results).where(red_team_results.c.id == "x001")
        ).mappings().one()
        # 描述被就地覆盖
        assert row["description"] == "审核者改写后的描述。"
        # 定档字段一个没动
        assert row["title"] == "生产 TERP 后台管理员会话"
        assert row["target_asset"] == "terp.t.kkguan.com"
        assert row["result_type"] == "permission"
        assert row["points_declared"] == 10
        assert row["points_awarded"] == 0
        assert row["review_reason"] == ""
        # 关键：状态没有被复位成 reviewing，也没有重新排队
        assert row["status"] == "reviewing"
        assert row["reviewed_by"] is None
        runs = conn.execute(
            select(vulnerability_review_runs).where(
                vulnerability_review_runs.c.task_id == task["id"]
            )
        ).mappings().all()
        assert len(runs) == 1, "改描述不应新建审核 run"

    # 就地覆盖的旧文会丢，所以必须留一笔可追溯的 audit 事件
    with engine.connect() as conn:
        events = conn.execute(
            select(audit_events).where(
                audit_events.c.task_id == task["id"],
                audit_events.c.event_type == "redteam.result.description_revised",
            )
        ).mappings().all()
    assert len(events) == 1, events
    payload = json.loads(events[0]["payload_json"])
    assert payload["changed_fields"] == ["description", "reproduction_json"]
    assert payload["steps_before"] == 1
    assert payload["steps_after"] == 1

    # 定档字段不在可写范围里
    for frozen in ("title", "points_declared", "result_type", "target_asset", "status"):
        rejected = client.post(
            f"{base}/description",
            json={**lease, "description": "再改一次", frozen: "x"},
        )
        assert rejected.status_code == 422, (frozen, rejected.text)

    # 红队成果没有前提/影响/修复建议三列
    for name in ("prerequisites", "impact", "remediation"):
        rejected = client.post(
            f"{base}/description",
            json={**lease, "description": "再改一次", name: "内容"},
        )
        assert rejected.status_code == 409, (name, rejected.text)

    # 步骤数与每步报文数不可增删
    for bad in (_reproduction(steps=2), _reproduction(exchanges=2)):
        rejected = client.post(
            f"{base}/description",
            json={**lease, "reproduction": bad},
        )
        assert rejected.status_code == 409, rejected.text
        assert "不可增删" in rejected.json()["error"]["message"], rejected.text

    # 改完之后这条审核仍然能提交裁决（状态没被踢掉）
    _attach_shots(client, task["id"], result_id="x001")
    # 图挂齐后再改写文本，闸门要求不变、也不会重新变缺
    after_shots = client.post(
        f"{base}/description",
        json={**lease, "description": "第二次改写。", "reproduction": _reproduction(title="再改一次文字")},
    )
    assert after_shots.status_code == 200, after_shots.text
    assert after_shots.json()["missing_packet_shots"] == []
    assert after_shots.json()["ready"] is True
    decision = client.post(
        f"{base}/decision",
        json={
            **lease,
            "decision": "verified",
            "points_awarded": 10,
            "reason": "描述已由审核者复核改写，证据与结论一致。",
        },
    )
    assert decision.status_code == 200, decision.text


def test_vulnerability_description_rewrite_refreshes_fingerprint_and_keeps_the_run_usable(
    client,
    engine,
) -> None:
    """漏洞侧：改完描述后指纹要刷新，status/decision/complete 三处都不能拦。"""
    task = _vuln_task(client)
    vulnerability = _seed_vulnerability(client, task["id"])
    base, lease = _open_vuln_review(client, task["id"], vulnerability["id"])

    revised = _reproduction(title="审核者改写后的步骤标题")
    revised["steps"][0]["exchanges"][0]["response"] = 'HTTP/1.1 200 OK\r\n\r\n{"code":1,"by":"reviewer"}'
    response = client.post(
        f"{base}/description",
        json={
            **lease,
            "description": "审核者改写后的描述。",
            "prerequisites": "需要任意低权限账号，且该账号对订单 2 有读权限。",
            "impact": "跨用户数据泄露，涉及订单详情。",
            "remediation": "服务端校验对象归属后再返回。",
            "reproduction": revised,
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["applied_fields"] == [
        "description",
        "impact",
        "prerequisites",
        "remediation",
        "reproduction_json",
    ]
    assert payload["vulnerability_id"] == vulnerability["id"]

    with engine.connect() as conn:
        row = conn.execute(
            select(vulnerabilities).where(vulnerabilities.c.id == vulnerability["id"])
        ).mappings().one()
        assert row["description"] == "审核者改写后的描述。"
        assert "订单 2" in row["prerequisites"]
        assert row["impact"] == "跨用户数据泄露，涉及订单详情。"
        assert row["remediation"] == "服务端校验对象归属后再返回。"
        # 定档字段没动
        assert row["title"] == "对象级授权缺失"
        assert row["severity"] == "high"
        assert row["affected_target"] == "https://review.example.com/api/orders/2"
        # 指纹必须跟着刷新，否则下一步裁决会被「审核期间对象变了」挡死
        run = conn.execute(
            select(vulnerability_review_runs).where(
                vulnerability_review_runs.c.id == base.rsplit("/", 1)[-1]
            )
        ).mappings().one()
        assert run["input_fingerprint"], run["input_fingerprint"]

    # 三处指纹校验都要过
    status = client.post(f"{base}/status", json=lease)
    assert status.status_code == 200, status.text
    assert "changed" not in status.text

    _attach_shots(client, task["id"], vulnerability_id=vulnerability["id"])
    decision = client.post(
        f"{base}/decision",
        json={
            **lease,
            "decision": "verified",
            "reason": "描述已由审核者复核改写，证据与结论一致。",
        },
    )
    assert decision.status_code == 200, decision.text

    with engine.connect() as conn:
        events = conn.execute(
            select(audit_events).where(
                audit_events.c.task_id == task["id"],
                audit_events.c.event_type == "vulnerability.description_revised",
            )
        ).mappings().all()
    assert len(events) == 1, events
    assert "description" in json.loads(events[0]["payload_json"])["changed_fields"]
