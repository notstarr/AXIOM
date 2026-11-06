"""Human review override for red-team Results.

A Reviewer's verdict is not always right — a session obtained through the bsk
channel should not count. The console needs a direct override that writes
status / reason / awarded points, cancels in-flight review runs and leaves an
audit trail. These tests pin that path, plus the detail-endpoint fix that keeps
the per-step packet shots visible after a Result is adjudicated.
"""

from __future__ import annotations

import json

from sqlalchemy import select

from axiom.modules.artifacts.tables import artifacts
from axiom.modules.audit.tables import audit_events
from axiom.modules.redteam.tables import red_team_chains, red_team_results
from axiom.modules.reports.tables import report_evidence
from axiom.modules.vulnerability_reviews.tables import vulnerability_review_runs
from axiom.shared.clock import utcnow


REPRODUCTION = {
    "entry_point": "GET /ierp/",
    "prerequisites": ["持有已认证会话"],
    "chain_gain": "定位到 KERP 生产入口",
    "steps": [
        {
            "title": "带真实会话访问 /ierp/",
            "request": "GET /ierp/ HTTP/1.1\r\nHost: cosmic.example.com\r\n\r\n",
            "response": "HTTP/1.1 302 Found\r\nLocation: /ierp/?formId=pc_main_console\r\n\r\n",
            "proves": "会话被判为已登录",
        }
    ],
}


def _task(client) -> dict:
    project = client.post("/api/v1/projects", json={"title": "人工审核"}).json()
    return client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "红队",
            "origin": "cosmic.example.com",
            "goal": "取得 100 积分",
            "engagement_mode": "red_team",
            "red_team_objective": {
                "initial_principal": {"id": "user:member"},
                "target_capabilities": [],
                "target_assets": ["cosmic.example.com"],
                "target_points": 100,
            },
        },
    ).json()


def _seed_result(engine, task_id: str, *, status: str = "verified", points: int = 35) -> None:
    with engine.begin() as conn:
        conn.execute(
            red_team_chains.insert().values(
                id="ch001", task_id=task_id, title="入口定位",
                initial_principal_json={"id": "user:member"},
                target_capabilities_json=[], target_assets_json=[],
                status="open", status_reason="", current_state_json={},
                source_fact_ids_json=[], source_intent_ids_json=[],
                created_by="human.operator", created_at=utcnow(), updated_at=utcnow(),
            )
        )
        conn.execute(
            red_team_results.insert().values(
                id="x001", task_id=task_id, chain_id="ch001", intent_id="i001",
                result_type="credential", title="KERP 已认证会话",
                description="带真实会话可进入工作台", principal="user:member", tenant="",
                capabilities_before_json=[], capabilities_gained_json=["kerp:session"],
                capabilities_after_json=[], target_asset="cosmic.example.com",
                data_fields_json=[], data_summary="", evidence_refs_json=[],
                reproduction_json=REPRODUCTION, status=status, reported_by="worker-a",
                points_declared=1, points_awarded=points, review_reason="复现完整",
                created_at=utcnow(), updated_at=utcnow(),
            )
        )


def _attach_shot(engine, task_id: str, *, step_index: int = 1) -> None:
    with engine.begin() as conn:
        conn.execute(
            artifacts.insert().values(
                id="a901", task_id=task_id, artifact_type="upload",
                storage_key=f"tasks/{task_id}/uploads/step-{step_index}-a.png",
                summary="", content_hash="0" * 64, size=1024,
                created_by="reviewer", created_at=utcnow(), archived=False,
            )
        )
        conn.execute(
            report_evidence.insert().values(
                task_id=task_id, subject_type="redteam_result", subject_id="x001",
                artifact_id="a901", step_index=step_index, kind="packet", caption="",
                display_order=0, created_by="reviewer", created_at=utcnow(),
            )
        )


def _override(client, task_id: str, payload: dict):
    return client.post(
        f"/api/v1/tasks/{task_id}/red-team/results/x001/human-review", json=payload
    )


def test_human_override_rejects_a_terminal_verified_result(client, engine) -> None:
    task = _task(client)
    _seed_result(engine, task["id"], status="verified", points=35)
    reason = "调用了 bsk 拿到了登录态不算"

    response = _override(client, task["id"], {
        "status": "rejected", "reason": reason, "actor": "hexin",
    })

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "rejected"
    assert body["points_awarded"] == 0
    assert body["reviewed_by"] == "human:hexin"
    assert body["review_reason"] == reason
    detail = client.get(f"/api/v1/tasks/{task['id']}/red-team/results/x001").json()
    assert detail["status"] == "rejected"
    assert detail["points_awarded"] == 0


def test_human_override_sets_awarded_points_on_verified(client, engine) -> None:
    task = _task(client)
    _seed_result(engine, task["id"], status="reviewing", points=0)

    response = _override(client, task["id"], {
        "status": "verified", "reason": "人工复核后核定 7 分",
        "points_awarded": 7, "actor": "hexin",
    })

    assert response.status_code == 200, response.text
    assert response.json()["points_awarded"] == 7


def test_human_override_requires_points_for_verified(client, engine) -> None:
    task = _task(client)
    _seed_result(engine, task["id"], status="reviewing", points=0)

    response = _override(client, task["id"], {
        "status": "verified", "reason": "人工复核后核定通过",
    })

    assert response.status_code == 409
    assert "积分数量" in response.text


def test_human_override_requires_a_real_reason(client, engine) -> None:
    task = _task(client)
    _seed_result(engine, task["id"])

    response = _override(client, task["id"], {"status": "rejected", "reason": "x"})

    assert response.status_code == 422


def test_human_override_cancels_active_review_run(client, engine) -> None:
    task = _task(client)
    _seed_result(engine, task["id"], status="reviewing", points=0)
    triggered = client.post(
        f"/api/v1/tasks/{task['id']}/red-team/results/x001/reviews",
        json={"actor": "human.operator", "reason": "复核"},
    )
    assert triggered.status_code == 201, triggered.text
    run_id = triggered.json()["id"]

    response = _override(client, task["id"], {
        "status": "rejected", "reason": "调用了 bsk 拿到了登录态不算",
    })
    assert response.status_code == 200, response.text

    with engine.connect() as conn:
        status = conn.execute(
            select(vulnerability_review_runs.c.status).where(
                vulnerability_review_runs.c.task_id == task["id"],
                vulnerability_review_runs.c.id == run_id,
            )
        ).scalar_one()
    assert status == "cancelled"


def test_human_override_writes_before_after_audit(client, engine) -> None:
    task = _task(client)
    _seed_result(engine, task["id"], status="verified", points=35)

    response = _override(client, task["id"], {
        "status": "rejected", "reason": "调用了 bsk 拿到了登录态不算", "actor": "hexin",
    })
    assert response.status_code == 200, response.text

    with engine.connect() as conn:
        payload = conn.execute(
            select(audit_events.c.payload_json).where(
                audit_events.c.task_id == task["id"],
                audit_events.c.event_type == "redteam.result.review_overridden",
            )
        ).scalar_one()
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert payload["before"]["status"] == "verified"
    assert payload["before"]["points_awarded"] == 35
    assert payload["after"] == {"status": "rejected", "points_awarded": 0}
    assert payload["reason"] == "调用了 bsk 拿到了登录态不算"
    assert payload["actor"] == "hexin"


def test_single_result_detail_carries_step_packet_shots(client, engine) -> None:
    """选中某条后单取详情不能把列表里已装配的每步截图抹成空数组。"""
    task = _task(client)
    _seed_result(engine, task["id"])
    _attach_shot(engine, task["id"], step_index=1)

    detail = client.get(f"/api/v1/tasks/{task['id']}/red-team/results/x001").json()
    listed = client.get(f"/api/v1/tasks/{task['id']}/red-team/results").json()[0]

    assert len(detail["evidence"]) == 1
    assert detail["evidence"][0]["kind"] == "packet"
    assert detail["evidence"][0]["step_index"] == 1
    assert listed["evidence"] == detail["evidence"]
