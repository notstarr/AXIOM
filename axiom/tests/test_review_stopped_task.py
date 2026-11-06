"""审核与「任务是否在跑」解耦：任务暂停后仍可重审已有成果。

背景：控制台点「重新审核」时，服务端在 ``trigger_result`` 里把请求排进队列，但
``enqueue_redteam_in`` 见任务不是 running 就静默返回 None，于是触发端报
「Task is not running; review was not queued」。任务停下之后重审一条已经定论、
但复核后觉得判错了的成果，本来是正当操作，不该被任务状态挡住。

放开范围只到 ``stopped``：任务暂停本来就意味着可以恢复继续跑，重审不破坏任何
终局结果。``completed``（已结束）与 ``failed`` 仍是终局，不重审 —— 那会把已定稿
的分数事后改写。

这条用例走完整条链：暂停任务 → 触发重审 → 领取 → 启动 → 提交裁决 → 收口完成，
确认每一层闸门都放行，而不是只放了第一层。
"""

from __future__ import annotations

from sqlalchemy import select

from axiom.modules.redteam.tables import red_team_chains, red_team_results
from axiom.shared.clock import utcnow


REPRODUCTION = {
    "entry_point": "POST /admin/index/info.html",
    "prerequisites": ["持有过期 JWT"],
    "steps": [
        {
            "title": "改 username=admin 落库",
            "request": (
                "POST /admin/index/info.html HTTP/1.1\r\n"
                "Host: terp.t.kkguan.com\r\n\r\n"
                '{"id":14473}'
            ),
            "response": 'HTTP/1.1 200 OK\r\n\r\n{"code":1}',
            "proves": "提权由 username 字段决定",
        }
    ],
}


def _task(client) -> dict:
    worker = client.put(
        "/api/v1/workers/worker-a",
        json={
            "capabilities": ["review"],
            "max_running": 4,
            "config": {"model": "review-model"},
        },
    )
    assert worker.status_code == 200, worker.text
    project = client.post("/api/v1/projects", json={"title": "暂停后重审"}).json()
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


def _seed_result(engine, task_id: str, result_id: str) -> None:
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
                description="改自身 username=admin 后取得生产 TERP 后台管理员会话",
                principal="user:sso",
                tenant="",
                capabilities_before_json=[],
                capabilities_gained_json=["app:admin"],
                capabilities_after_json=[],
                target_asset="terp.t.kkguan.com",
                data_fields_json=[],
                data_summary="",
                evidence_refs_json=[],
                reproduction_json=REPRODUCTION,
                # 已经定过论：复核后觉得判低了，要求重审
                status="verified",
                reported_by="worker-a",
                points_declared=10,
                points_awarded=5,
                reviewed_by="reviewer",
                review_reason="初审判低了",
                created_at=utcnow(),
                updated_at=utcnow(),
            )
        )


def test_stopped_task_can_be_re_reviewed_end_to_end(client, engine) -> None:
    task = _task(client)
    _seed_result(engine, task["id"], "x001")

    stopped = client.post(f"/api/v1/tasks/{task['id']}/stop")
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["status"] == "stopped"

    triggered = client.post(
        f"/api/v1/tasks/{task['id']}/red-team/results/x001/reviews",
        json={"actor": "human.operator", "reason": "初审判低了，要求重审"},
    )
    assert triggered.status_code == 201, triggered.text
    run_id = triggered.json()["id"]
    base = f"/api/v1/dispatch/tasks/{task['id']}/vulnerability-review-runs/{run_id}"

    # 领取 / 启动：停了的任务也要能被审核 worker 拿到
    claimed = client.post(f"{base}/claim", json={"worker_name": "worker-a"})
    assert claimed.status_code == 200, claimed.text
    lease = {"lease_id": claimed.json()["lease_id"]}
    assert client.post(f"{base}/start", json=lease).status_code == 200
    assert client.post(f"{base}/input", json=lease).status_code == 200

    shot = client.post(
        f"/api/v1/tasks/{task['id']}/red-team/results/x001/evidence/capture",
        files={"file": ("step-01.png", b"\x89PNG\r\n\x1a\n", "image/png")},
        data={"step_index": "1", "kind": "packet", "caption": "步骤 1 回包"},
    )
    assert shot.status_code == 201, shot.text

    decision = client.post(
        f"{base}/decision",
        json={
            **lease,
            "decision": "verified",
            "points_awarded": 10,
            "reason": "复核后确认属于核心应用管理员，按 10 分核定。",
        },
    )
    assert decision.status_code == 200, decision.text

    assert client.post(f"{base}/conclude", json=lease).status_code in {200, 204}
    completed = client.post(f"{base}/complete", json=lease)
    assert completed.status_code == 200, completed.text

    with engine.connect() as conn:
        row = conn.execute(
            select(red_team_results).where(red_team_results.c.id == "x001")
        ).mappings().one()
    assert row["status"] == "verified"
    assert row["points_awarded"] == 10
    # 任务本身没有被这次审核改回运行态
    tasks = client.get(f"/api/v1/tasks/{task['id']}").json()
    assert tasks["status"] == "stopped"


def test_completed_task_still_refuses_a_new_review(client, engine) -> None:
    """已完成是终局：重审会改写已定稿的分数，继续拒绝。"""
    task = _task(client)
    _seed_result(engine, task["id"], "x001")
    with engine.begin() as conn:
        from axiom.modules.tasks.tables import tasks as tasks_table

        conn.execute(
            tasks_table.update()
            .where(tasks_table.c.id == task["id"])
            .values(status="completed", completed_at=utcnow())
        )

    response = client.post(
        f"/api/v1/tasks/{task['id']}/red-team/results/x001/reviews",
        json={"actor": "human.operator", "reason": "要求重审"},
    )
    assert response.status_code == 409, response.text
    assert "Review was not queued" in response.text
