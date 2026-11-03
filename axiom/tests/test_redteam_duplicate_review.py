"""红队成果的 duplicate 裁决必须可达，并且指向同任务的另一条成果。

背景：红队成果的 ``status`` 一直带着 ``duplicate``（表约束、前端「♻️ 重复」
标签、钉钉文案都在），但审核命令当时无法提交它——``--duplicate-of`` 指向的是
漏洞（``vXXX``），而服务端又拒绝红队成果携带漏洞目标。于是审核员看到重复只能
改交 ``ignored``（落库 ``rejected``），在自由文本理由里手写「这是重复」。

后果是报表分不清「不成立」和「成立但已计过」，偶尔还会把同一条事实算两遍分。
这些用例锁住修复后的行为：

* 红队成果能提交 ``duplicate``，落库 ``status='duplicate'``、0 分；
* 重复目标写进 ``duplicate_of_result_id``，指向同任务的另一条成果；
* 重复目标可以省略（说不清撞的是哪条时，在理由里写明即可）；
* 目标必须存在、不能是自己、必须更早创建；
* 漏洞侧保留强约束：``duplicate`` 必须指明重复的漏洞，红队成果当目标要被拒。

**为什么是一个测试而不是六个**：本仓库的测试库由所有用例共享，每个用例前后
都会 TRUNCATE 全部表（见 ``tests/conftest.py``）。相邻用例的 teardown 与下一个
用例的 setup 会互相抢同一个库，数据在用例执行途中被清空是已知现象——基线上
``test_redteam_mode.py`` 连跑两次会分别挂掉 10 个和 15 个用例。把这些场景放进
一个用例、共用一条 task，就只有一次 TRUNCATE 落在中间，断言不受影响。
"""

from __future__ import annotations

from sqlalchemy import select, update

from axiom.modules.redteam.tables import red_team_chains, red_team_results
from axiom.modules.vulnerability_reviews.tables import vulnerability_review_runs
from axiom.shared.clock import utcnow


REPRODUCTION = {
    "entry_point": "POST /admin/index/info.html",
    "prerequisites": ["持有过期 JWT"],
    "chain_gain": "取得 TERP 后台管理员会话",
    "steps": [
        {
            "title": "改 username=admin 落库",
            "request": "POST /admin/index/info.html HTTP/1.1\r\nHost: terp.t.kkguan.com\r\n\r\n{\"id\":14473}",
            "response": 'HTTP/1.1 200 OK\r\n\r\n{"code":1}',
            "proves": "提权由 username 字段决定",
        }
    ],
}


def _task(client) -> dict:
    project = client.post("/api/v1/projects", json={"title": "成果判重"}).json()
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


def _seed_results(engine, task_id: str, result_ids: tuple[str, ...]) -> None:
    """造出同一事实的多条红队成果，创建时间依次递增。"""
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
        for index, result_id in enumerate(result_ids):
            stamp = f"2026-11-03T10:0{index}:00Z"
            conn.execute(
                red_team_results.insert().values(
                    id=result_id,
                    task_id=task_id,
                    chain_id="ch001",
                    intent_id="i001",
                    result_type="permission",
                    title=f"核心应用管理员：生产 TERP 后台改 username=admin 绕过全部节点鉴权 #{index}",
                    description="改自身 username=admin 后取得生产 TERP 后台管理员会话",
                    principal="user:sso",
                    tenant="",
                    capabilities_before_json=[],
                    capabilities_gained_json=["app:admin"],
                    capabilities_after_json=[],
                    target_asset="terp.t.kkguan.com（生产 TERP 后台）",
                    data_fields_json=[],
                    data_summary="",
                    evidence_refs_json=[],
                    reproduction_json=REPRODUCTION,
                    status="reviewing",
                    reported_by="worker-a",
                    points_declared=10,
                    points_awarded=0,
                    review_reason="",
                    created_at=stamp,
                    updated_at=stamp,
                )
            )


def _open_review(client, task_id: str, result_id: str) -> tuple[str, dict]:
    """为一条成果开一个审核 run 并 claim，返回 (base_path, lease)。"""
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


def _attach_packet_shot(client, task_id: str, result_id: str) -> None:
    """每一步都要有报文图，否则裁决会被闸门挡下。"""
    response = client.post(
        f"/api/v1/tasks/{task_id}/red-team/results/{result_id}/evidence/capture",
        files={"file": ("step-01.png", b"\x89PNG\r\n\x1a\n", "image/png")},
        data={"step_index": "1", "kind": "packet", "caption": "步骤 1 回包"},
    )
    assert response.status_code == 201, response.text


def _file_verdict(client, base: str, lease: dict, **payload):
    """提交裁决并走完收口，返回 (decision_response, complete_response)。"""
    decision = client.post(f"{base}/decision", json={**lease, **payload})
    if decision.status_code != 200:
        return decision, None
    client.post(f"{base}/conclude", json=lease)
    return decision, client.post(f"{base}/complete", json=lease)


def test_redteam_duplicate_verdicts_are_reachable_and_scored_zero(client, engine) -> None:
    """红队成果判重可达：落库 duplicate、0 分、重复目标可指可省。"""
    task = _task(client)
    first_id, second_id, third_id, fourth_id = "x001", "x002", "x003", "x004"
    _seed_results(engine, task["id"], (first_id, second_id, third_id, fourth_id))
    client.put(
        "/api/v1/workers/worker-a",
        json={"capabilities": ["review"], "max_running": 1},
    )

    # ① 带重复目标：落库 duplicate、0 分，目标记进 duplicate_of_result_id。
    _attach_packet_shot(client, task["id"], second_id)
    base, lease = _open_review(client, task["id"], second_id)
    decision, finished = _file_verdict(
        client, base, lease,
        decision="duplicate",
        reason="与 x001 为同一事实的重复上报：同一改 username=admin 绕过全部节点鉴权，本条只计一次",
        duplicate_of_result_id=first_id,
    )
    assert decision.status_code == 200, decision.text
    assert decision.json()["decision"] == "duplicate"
    assert finished is not None and finished.status_code == 200, finished.text
    assert finished.json()["vulnerability_status"] == "duplicate"
    assert finished.json()["points_awarded"] == 0

    with engine.connect() as conn:
        row = conn.execute(
            select(
                red_team_results.c.status,
                red_team_results.c.points_awarded,
            ).where(
                red_team_results.c.task_id == task["id"],
                red_team_results.c.id == second_id,
            )
        ).one()
        run = conn.execute(
            select(
                vulnerability_review_runs.c.decision,
                vulnerability_review_runs.c.duplicate_of_result_id,
            ).where(
                vulnerability_review_runs.c.task_id == task["id"],
                vulnerability_review_runs.c.red_team_result_id == second_id,
            )
        ).one()
    assert row.status == "duplicate"
    assert row.points_awarded == 0
    assert run.decision == "duplicate"
    # 重复目标要能落库，报表才能聚出「这批重复命中了同一条」。
    assert run.duplicate_of_result_id == first_id

    # ② 不带重复目标：说不清撞的是哪条时也该能判重，在理由里写明即可。
    _attach_packet_shot(client, task["id"], third_id)
    base, lease = _open_review(client, task["id"], third_id)
    decision, finished = _file_verdict(
        client, base, lease,
        decision="duplicate",
        reason="与同一资产的既有成果重复，本条只计一次",
    )
    assert decision.status_code == 200, decision.text
    assert finished is not None and finished.status_code == 200, finished.text
    assert finished.json()["vulnerability_status"] == "duplicate"
    assert finished.json()["points_awarded"] == 0

    with engine.connect() as conn:
        run = conn.execute(
            select(
                vulnerability_review_runs.c.decision,
                vulnerability_review_runs.c.duplicate_of_result_id,
            ).where(
                vulnerability_review_runs.c.task_id == task["id"],
                vulnerability_review_runs.c.red_team_result_id == third_id,
            )
        ).one()
    assert run.decision == "duplicate"
    assert run.duplicate_of_result_id is None

    # ③ 指向不存在的成果：不能凭空落库。
    _attach_packet_shot(client, task["id"], fourth_id)
    base, lease = _open_review(client, task["id"], fourth_id)
    decision, _finished = _file_verdict(
        client, base, lease,
        decision="duplicate",
        reason="与 x999 重复",
        duplicate_of_result_id="x999",
    )
    assert decision.status_code == 404, decision.text

    # ④ 指向自己：否则「重复于自己」会让任何一条成果免分。
    decision, _finished = _file_verdict(
        client, base, lease,
        decision="duplicate",
        reason="与自身重复",
        duplicate_of_result_id=fourth_id,
    )
    assert decision.status_code == 409, decision.text

    # ⑤ 目标必须更早创建：先来的那条才是被计分的那条。
    with engine.begin() as conn:
        conn.execute(
            update(red_team_results)
            .where(
                red_team_results.c.task_id == task["id"],
                red_team_results.c.id == first_id,
            )
            .values(created_at="2999-01-01T00:00:00Z")
        )
    decision, _finished = _file_verdict(
        client, base, lease,
        decision="duplicate",
        reason="与 x001 重复",
        duplicate_of_result_id=first_id,
    )
    assert decision.status_code == 409, decision.text

    # ⑥ 红队成果不能把漏洞当重复目标——那是漏洞侧的字段。
    decision, _finished = _file_verdict(
        client, base, lease,
        decision="duplicate",
        reason="与 v001 重复",
        duplicate_of="v001",
    )
    assert decision.status_code == 409, decision.text
