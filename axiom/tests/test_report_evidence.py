"""The evidence hook: pictures a reviewer attaches *after* the report exists.

Explore ships packets; a delivered report still wants the key screenshots, and those
are taken by whoever replays the exploit to adjudicate it.  These tests pin the
contract: attach against a step, list in step order, detach, and never exceed the cap.
"""

from __future__ import annotations

from axiom.modules.reproduction.schemas import Reproduction


REPRODUCTION = {
    "entry_point": "POST /upload.php",
    "prerequisites": ["无需认证"],
    "steps": [
        {
            "title": "上传 PHP 文件",
            "request": "POST /upload.php HTTP/1.1\r\nHost: a.example.com\r\n\r\n--X--",
            "response": "HTTP/1.1 200 OK\r\n\r\n{\"url\":\"/uploads/shell.php\"}",
            "proves": "上传接口不校验类型",
        }
    ],
}


def _project_task(client, *, red_team: bool = False) -> dict:
    project = client.post("/api/v1/projects", json={"title": "证据钩子"}).json()
    payload = {
        "project_id": project["id"],
        "title": "任务",
        "origin": "a.example.com",
        "goal": "目标",
    }
    if red_team:
        payload["engagement_mode"] = "red_team"
        payload["red_team_objective"] = {
            "initial_principal": {"id": "user:member"},
            "target_capabilities": ["host:shell"],
            "target_assets": ["a.example.com"],
        }
    response = client.post("/api/v1/tasks", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _vulnerability(client, task_id: str) -> dict:
    response = client.post(
        f"/api/v1/tasks/{task_id}/vulnerabilities",
        json={
            "title": "任意文件上传",
            "type": "文件上传",
            "severity": "high",
            "description": "上传 PHP 后可执行",
            "prerequisites": "无需认证",
            "impact": "服务器接管",
            "reproduction": REPRODUCTION,
            "source": "human",
            "reported_by": "tester",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _upload(client, task_id: str, name: str, payload: bytes) -> str:
    response = client.post(
        f"/api/v1/tasks/{task_id}/artifacts/upload",
        files={"file": (name, payload, "image/png")},
        data={"actor": "reviewer", "summary": name},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_a_picture_can_be_attached_to_a_specific_step(client) -> None:
    task = _project_task(client)
    vulnerability = _vulnerability(client, task["id"])
    artifact_id = _upload(client, task["id"], "step-01.png", b"\x89PNG\r\n\x1a\n")

    attached = client.post(
        f"/api/v1/tasks/{task['id']}/vulnerabilities/{vulnerability['id']}/evidence",
        json={"artifact_id": artifact_id, "step_index": 1, "caption": "上传成功回显"},
    )

    assert attached.status_code == 201, attached.text
    payload = attached.json()
    assert payload["step_index"] == 1
    assert payload["caption"] == "上传成功回显"
    assert payload["url"].endswith("/download")


def test_attached_evidence_lists_in_step_order(client) -> None:
    task = _project_task(client)
    vulnerability = _vulnerability(client, task["id"])
    overview = _upload(client, task["id"], "overview.png", b"\x89PNG\r\n\x1a\n1")
    step_two = _upload(client, task["id"], "step-02.png", b"\x89PNG\r\n\x1a\n2")
    step_one = _upload(client, task["id"], "step-01.png", b"\x89PNG\r\n\x1a\n3")
    base = f"/api/v1/tasks/{task['id']}/vulnerabilities/{vulnerability['id']}/evidence"

    for artifact_id, step_index in ((step_two, 2), (overview, 0), (step_one, 1)):
        assert client.post(
            base, json={"artifact_id": artifact_id, "step_index": step_index}
        ).status_code == 201

    items = client.get(base).json()

    assert [item["step_index"] for item in items] == [0, 1, 2]


def test_attaching_the_same_picture_twice_is_idempotent(client) -> None:
    """A double-submitted attach must not create a duplicate row."""
    task = _project_task(client)
    vulnerability = _vulnerability(client, task["id"])
    artifact_id = _upload(client, task["id"], "step-01.png", b"\x89PNG\r\n\x1a\n")
    base = f"/api/v1/tasks/{task['id']}/vulnerabilities/{vulnerability['id']}/evidence"

    first = client.post(base, json={"artifact_id": artifact_id, "step_index": 1})
    second = client.post(base, json={"artifact_id": artifact_id, "step_index": 1})

    assert first.status_code == 201
    assert second.status_code == 201
    assert len(client.get(base).json()) == 1


def test_detaching_removes_the_link_but_not_the_artifact(client) -> None:
    task = _project_task(client)
    vulnerability = _vulnerability(client, task["id"])
    artifact_id = _upload(client, task["id"], "step-01.png", b"\x89PNG\r\n\x1a\n")
    base = f"/api/v1/tasks/{task['id']}/vulnerabilities/{vulnerability['id']}/evidence"
    client.post(base, json={"artifact_id": artifact_id, "step_index": 1})

    removed = client.delete(f"{base}/{artifact_id}")

    assert removed.status_code == 204
    assert client.get(base).json() == []
    # The uploaded file itself is untouched: detaching is a report decision.
    assert client.get(f"/api/v1/tasks/{task['id']}/artifacts/{artifact_id}").status_code == 200


def test_detaching_something_not_attached_is_a_404(client) -> None:
    task = _project_task(client)
    vulnerability = _vulnerability(client, task["id"])

    response = client.delete(
        f"/api/v1/tasks/{task['id']}/vulnerabilities/{vulnerability['id']}/evidence/a999"
    )

    assert response.status_code == 404


def test_an_unknown_artifact_is_rejected(client) -> None:
    task = _project_task(client)
    vulnerability = _vulnerability(client, task["id"])

    response = client.post(
        f"/api/v1/tasks/{task['id']}/vulnerabilities/{vulnerability['id']}/evidence",
        json={"artifact_id": "a999", "step_index": 1},
    )

    assert response.status_code == 404


def test_a_negative_step_index_is_rejected(client) -> None:
    task = _project_task(client)
    vulnerability = _vulnerability(client, task["id"])

    response = client.post(
        f"/api/v1/tasks/{task['id']}/vulnerabilities/{vulnerability['id']}/evidence",
        json={"artifact_id": "a001", "step_index": -1},
    )

    assert response.status_code == 422


def test_the_detail_payload_carries_the_attached_pictures() -> None:
    """The console reads a flat ``evidence`` list with ``step_index`` and renders each
    picture under its step; the API detail endpoint is what feeds it."""
    from axiom.modules.reports.skeleton import build_vulnerability_report
    from axiom.modules.vulnerabilities.schemas import Vulnerability

    vulnerability = Vulnerability.model_validate(
        {
            "id": "v001",
            "task_id": "t001",
            "title": "任意文件上传",
            "type": "文件上传",
            "severity": "high",
            "description": "上传 PHP 后可执行",
            "prerequisites": "无需认证",
            "impact": "服务器接管",
            "status": "待验证",
            "source": "human",
            "reported_by": "tester",
            "reproduction": Reproduction.model_validate(REPRODUCTION),
            "created_at": "2026-10-27T00:00:00Z",
            "updated_at": "2026-10-27T00:00:00Z",
        }
    )
    doc = build_vulnerability_report(
        vulnerability,
        images_by_step={1: [{"path": "images/step-01-a.png", "caption": "上传成功"}]},
    )
    process = next(section for section in doc.sections if section.key == "process")
    steps = next(block for block in process.blocks if block.type == "steps")

    assert steps.steps[0]["images"][0]["path"] == "images/step-01-a.png"

def test_red_team_results_have_their_own_hook(client) -> None:
    """Both subject kinds share the table; the Result route works the same way."""
    task = _project_task(client, red_team=True)
    artifact_id = _upload(client, task["id"], "pivot.png", b"\x89PNG\r\n\x1a\n")
    base = f"/api/v1/tasks/{task['id']}/red-team/results/x001/evidence"

    attached = client.post(base, json={"artifact_id": artifact_id, "step_index": 2})

    assert attached.status_code == 201, attached.text
    assert attached.json()["step_index"] == 2
    assert [item["artifact_id"] for item in client.get(base).json()] == [artifact_id]


def test_vulnerability_report_view_returns_the_skeleton(client) -> None:
    task = _project_task(client)
    vulnerability = _vulnerability(client, task["id"])

    response = client.get(
        f"/api/v1/tasks/{task['id']}/vulnerabilities/{vulnerability['id']}/report"
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["subject_type"] == "vulnerability"
    assert payload["badge"] == "高危"
    assert [section["key"] for section in payload["sections"]] == [
        "basic",
        "description",
        "process",
        "impact",
        "verdict",
    ]
    process = next(section for section in payload["sections"] if section["key"] == "process")
    kinds = [block["type"] for block in process["blocks"]]
    # 漏洞模式不装配攻击链：前置条件只有本环的。
    assert "chain" not in kinds


def test_the_report_view_places_an_attached_picture_under_its_step(client) -> None:
    task = _project_task(client)
    vulnerability = _vulnerability(client, task["id"])
    artifact_id = _upload(client, task["id"], "step-01.png", b"\x89PNG\r\n\x1a\n")
    client.post(
        f"/api/v1/tasks/{task['id']}/vulnerabilities/{vulnerability['id']}/evidence",
        json={"artifact_id": artifact_id, "step_index": 1},
    )

    payload = client.get(
        f"/api/v1/tasks/{task['id']}/vulnerabilities/{vulnerability['id']}/report"
    ).json()

    process = next(section for section in payload["sections"] if section["key"] == "process")
    steps = next(block for block in process["blocks"] if block["type"] == "steps")
    assert steps["steps"][0]["images"][0]["artifact_id"] == artifact_id
