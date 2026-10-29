"""Red-team Results can be reviewed again and edited by a human.

A Result is reported by a worker and adjudicated by a reviewer, but the row still
belongs to the engagement: a wrong award needs a second look, and a typo in a packet
header makes the reproduction unreproducible. Both paths exist for vulnerabilities;
these pin the equivalents for Results, including the rule that editing substance
re-opens the verdict while editing the self-assessed score does not.
"""

from __future__ import annotations

import pytest

from axiom.modules.redteam.tables import red_team_chains, red_team_results
from axiom.shared.clock import utcnow


REPRODUCTION = {
    "entry_point": "POST /api/member/role",
    "prerequisites": ["持有普通成员会话"],
    "chain_gain": "取得 app:admin",
    "steps": [
        {
            "title": "创建管理员角色",
            "request": "POST /api/member/role HTTP/1.1\r\nHost: a.example.com\r\n\r\n{}",
            "response": "HTTP/1.1 200 OK\r\n\r\n{\"code\":1000}",
            "proves": "垂直越权成立",
        }
    ],
}


def _task(client, *, running: bool = True) -> dict:
    project = client.post("/api/v1/projects", json={"title": "成果审核"}).json()
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
    return task


def _seed_result(engine, task_id: str, *, status: str = "verified", points: int = 35) -> None:
    with engine.begin() as conn:
        conn.execute(
            red_team_chains.insert().values(
                id="ch001",
                task_id=task_id,
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
                task_id=task_id,
                chain_id="ch001",
                intent_id="i001",
                result_type="permission",
                title="取得成员管理能力",
                description="复用普通成员会话调用成员管理接口成功",
                principal="user:member",
                tenant="",
                capabilities_before_json=[],
                capabilities_gained_json=["tenant:admin"],
                capabilities_after_json=[],
                target_asset="a.example.com",
                data_fields_json=[],
                data_summary="",
                evidence_refs_json=[],
                reproduction_json=REPRODUCTION,
                status=status,
                reported_by="worker-a",
                points_declared=30,
                points_awarded=points,
                review_reason="复现完整",
                created_at=utcnow(),
                updated_at=utcnow(),
            )
        )


def test_a_verified_result_can_be_sent_back_for_review(client, engine) -> None:
    """A wrong award is worse than a late one, so a terminal Result can be reopened."""
    task = _task(client)
    _seed_result(engine, task["id"], status="verified", points=35)

    response = client.post(
        f"/api/v1/tasks/{task['id']}/red-team/results/x001/reviews",
        json={"actor": "human.operator", "reason": "分数偏高，需要复核"},
    )

    assert response.status_code == 201, response.text
    run = response.json()
    assert run["status"] == "pending"
    assert run["review_subject_type"] == "redteam_result"

    result = client.get(f"/api/v1/tasks/{task['id']}/red-team/results").json()[0]
    assert result["status"] == "reviewing"
    # 旧分数保留在行上供审计；重审期间该成果不再计入目标分（goals 只累加 verified）
    assert result["points_awarded"] == 35
    assert result["review_reason"] == ""


def test_triggering_a_second_review_while_one_is_active_is_rejected(client, engine) -> None:
    task = _task(client)
    _seed_result(engine, task["id"])

    first = client.post(
        f"/api/v1/tasks/{task['id']}/red-team/results/x001/reviews",
        json={"actor": "human.operator", "reason": "复核"},
    )
    assert first.status_code == 201, first.text
    second = client.post(
        f"/api/v1/tasks/{task['id']}/red-team/results/x001/reviews",
        json={"actor": "human.operator", "reason": "再来一次"},
    )

    assert second.status_code == 409
    assert "active ReviewRun" in second.text


def test_review_history_for_a_result_is_listed(client, engine) -> None:
    task = _task(client)
    _seed_result(engine, task["id"])

    client.post(
        f"/api/v1/tasks/{task['id']}/red-team/results/x001/reviews",
        json={"actor": "human.operator", "reason": "复核"},
    )
    listing = client.get(f"/api/v1/tasks/{task['id']}/red-team/results/x001/reviews")

    assert listing.status_code == 200, listing.text
    runs = listing.json()
    assert len(runs) == 1
    assert runs[0]["trigger"] == "manual"


def test_reviewing_an_unknown_result_is_a_404(client) -> None:
    task = _task(client)

    response = client.post(
        f"/api/v1/tasks/{task['id']}/red-team/results/x999/reviews",
        json={"actor": "human.operator", "reason": "复核"},
    )

    assert response.status_code == 404


def test_editing_substance_reopens_the_verdict(client, engine) -> None:
    """The reproduction is the evidence, so changing it invalidates the award."""
    task = _task(client)
    _seed_result(engine, task["id"], status="verified", points=35)

    response = client.patch(
        f"/api/v1/tasks/{task['id']}/red-team/results/x001",
        json={"title": "取得成员管理能力（已修正目标资产）"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["title"] == "取得成员管理能力（已修正目标资产）"
    assert body["status"] == "reviewing"
    assert body["review_reason"] == ""

    runs = client.get(f"/api/v1/tasks/{task['id']}/red-team/results/x001/reviews").json()
    assert len(runs) == 1
    assert runs[0]["trigger"] == "updated"
    assert "title" in runs[0]["trigger_payload"]["changed_fields"]


def test_editing_only_the_declared_score_keeps_the_verdict(client, engine) -> None:
    """points_declared is the reporter's self-assessment; the verdict is untouched."""
    task = _task(client)
    _seed_result(engine, task["id"], status="verified", points=35)

    response = client.patch(
        f"/api/v1/tasks/{task['id']}/red-team/results/x001",
        json={"points_declared": 40},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["points_declared"] == 40
    assert body["status"] == "verified"
    assert body["points_awarded"] == 35
    assert client.get(f"/api/v1/tasks/{task['id']}/red-team/results/x001/reviews").json() == []


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "verified"},
        {"points_awarded": 99},
        {"reviewed_by": "human.operator"},
        {"review_reason": "我自己批准"},
        {"chain_id": "ch999"},
        {"intent_id": "i999"},
    ],
)
def test_identity_and_verdict_fields_cannot_be_edited(client, engine, payload: dict) -> None:
    """Editing content is allowed; editing the verdict is not — that is the reviewer's.

    ``extra="forbid"`` turns these into 422 instead of silently dropping them, so a
    caller cannot believe it changed something it did not.
    """
    task = _task(client)
    _seed_result(engine, task["id"])

    response = client.patch(
        f"/api/v1/tasks/{task['id']}/red-team/results/x001", json=payload
    )

    assert response.status_code == 422


def test_editing_a_result_on_an_archived_task_is_rejected(client, engine) -> None:
    task = _task(client)
    _seed_result(engine, task["id"])
    with engine.begin() as conn:
        from sqlalchemy import update

        from axiom.modules.tasks.tables import tasks

        conn.execute(update(tasks).where(tasks.c.id == task["id"]).values(archived=True))

    response = client.patch(
        f"/api/v1/tasks/{task['id']}/red-team/results/x001", json={"title": "改个标题"}
    )

    assert response.status_code == 409
    assert "归档" in response.text


def test_editing_a_reproduction_replaces_it(client, engine) -> None:
    task = _task(client)
    _seed_result(engine, task["id"])

    updated_repro = dict(REPRODUCTION)
    updated_repro["entry_point"] = "POST /api/member/role/v2"
    response = client.patch(
        f"/api/v1/tasks/{task['id']}/red-team/results/x001",
        json={"reproduction": updated_repro},
    )

    assert response.status_code == 200, response.text
    assert response.json()["reproduction"]["entry_point"] == "POST /api/member/role/v2"


def test_editing_with_no_fields_is_a_no_op(client, engine) -> None:
    task = _task(client)
    _seed_result(engine, task["id"], status="verified", points=35)

    response = client.patch(f"/api/v1/tasks/{task['id']}/red-team/results/x001", json={})

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "verified"


def test_a_result_verdict_waits_for_the_step_packet_shots(client, engine) -> None:
    """成果的每一步也要有报文图。

    漏洞侧与成果侧共用一条闸门：成果的自评分最容易虚高，而「每一步的回包」是唯一
    不靠自述的东西。缺图时裁决必须被拒，补一张就放行一张。
    """
    task = _task(client)
    _seed_result(engine, task["id"], status="reviewing")
    client.put(
        "/api/v1/workers/worker-a",
        json={"capabilities": ["review"], "max_running": 1},
    )
    triggered = client.post(
        f"/api/v1/tasks/{task['id']}/red-team/results/x001/reviews",
        json={"actor": "human.operator", "reason": "复核"},
    )
    assert triggered.status_code == 201, triggered.text
    run_id = triggered.json()["id"]
    base = f"/api/v1/dispatch/tasks/{task['id']}/vulnerability-review-runs/{run_id}"
    claimed = client.post(f"{base}/claim", json={"worker_name": "worker-a"})
    assert claimed.status_code == 200, claimed.text
    lease = {"lease_id": claimed.json()["lease_id"]}
    assert client.post(f"{base}/start", json=lease).status_code == 200

    refused = client.post(
        f"{base}/decision",
        json={**lease, "decision": "verified", "reason": "证据充分。", "points_awarded": 20},
    )

    assert refused.status_code == 409, refused.text
    assert "步骤 1 缺少报文截图" in refused.text
    assert client.post(
        f"/api/v1/tasks/{task['id']}/red-team/results/x001/evidence/capture",
        files={"file": ("step-01.png", b"\x89PNG\r\n\x1a\n", "image/png")},
        data={"step_index": "1", "kind": "packet", "caption": "命令回显"},
    ).status_code == 201

    decided = client.post(
        f"{base}/decision",
        json={**lease, "decision": "verified", "reason": "证据充分。", "points_awarded": 20},
    )

    assert decided.status_code == 200, decided.text
    assert decided.json()["decision"] == "verified"
    assert decided.json()["ready"] is True
