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


def test_the_list_payload_carries_per_step_pictures_too(client) -> None:
    """The console renders the report straight from the list row.

    Fetching evidence only on the detail endpoint meant a picture attached while the
    list was already loaded never appeared — the pane showed the step but no image.
    """
    task = _project_task(client)
    vulnerability = _vulnerability(client, task["id"])
    artifact_id = _upload(client, task["id"], "step-01.png", b"\x89PNG\r\n\x1a\n")
    client.post(
        f"/api/v1/tasks/{task['id']}/vulnerabilities/{vulnerability['id']}/evidence",
        json={"artifact_id": artifact_id, "step_index": 1},
    )

    listed = client.get(f"/api/v1/tasks/{task['id']}/vulnerabilities").json()

    row = next(item for item in listed if item["id"] == vulnerability["id"])
    assert [item["artifact_id"] for item in row["evidence"]] == [artifact_id]
    assert row["evidence"][0]["step_index"] == 1


# --------------------------------------------------------------- packet vs business

TWO_STEPS = {
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
            "title": "访问落地的 shell",
            "request": "GET /uploads/shell.php?c=id HTTP/1.1\r\nHost: a.example.com\r\n\r\n",
            "response": "HTTP/1.1 200 OK\r\n\r\nuid=0(root)",
            "proves": "落地的文件被当作代码执行",
        },
    ],
}


def _two_step_vulnerability(client, task_id: str) -> dict:
    response = client.post(
        f"/api/v1/tasks/{task_id}/vulnerabilities",
        json={
            "title": "任意文件上传",
            "type": "文件上传",
            "severity": "critical",
            "description": "上传 PHP 后可执行",
            "prerequisites": "无需认证",
            "impact": "服务器接管",
            "reproduction": TWO_STEPS,
            "source": "human",
            "reported_by": "tester",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_the_default_kind_is_business_so_nothing_silently_counts_as_a_packet(client) -> None:
    """A caller that says nothing must not satisfy the packet gate.

    Historical rows and hand-attached pictures predate the distinction; treating silence
    as ``packet`` would let them claim a step was proven when no response was ever shown.
    """
    task = _project_task(client)
    vulnerability = _vulnerability(client, task["id"])
    artifact_id = _upload(client, task["id"], "step-01.png", b"\x89PNG\r\n\x1a\n")

    attached = client.post(
        f"/api/v1/tasks/{task['id']}/vulnerabilities/{vulnerability['id']}/evidence",
        json={"artifact_id": artifact_id, "step_index": 1},
    )

    assert attached.status_code == 201, attached.text
    assert attached.json()["kind"] == "business"


def test_a_packet_shot_needs_a_real_step(client) -> None:
    """step_index 越界要被拒，并且报错要点名真实步数——不然审核者只能靠猜。"""
    task = _project_task(client)
    vulnerability = _two_step_vulnerability(client, task["id"])

    response = client.post(
        f"/api/v1/tasks/{task['id']}/vulnerabilities/{vulnerability['id']}/evidence",
        json={"artifact_id": _upload(client, task["id"], "x.png", b"\x89PNG\r\n\x1a\n"),
              "step_index": 3, "kind": "packet"},
    )

    assert response.status_code == 409, response.text
    assert "2" in response.text and "1..2" in response.text


def test_a_packet_shot_is_refused_when_there_is_no_reproduction(client) -> None:
    task = _project_task(client)
    vulnerability = _vulnerability(client, task["id"])
    # 手工录入的漏洞没有复现步骤：没有「某一步」可以挂报文图
    manual = client.post(
        f"/api/v1/tasks/{task['id']}/vulnerabilities",
        json={
            "title": "手工录入",
            "type": "其他",
            "severity": "low",
            "description": "无复现",
            "impact": "未知",
            "source": "human",
            "reported_by": "tester",
        },
    ).json()

    response = client.post(
        f"/api/v1/tasks/{task['id']}/vulnerabilities/{manual['id']}/evidence",
        json={"artifact_id": _upload(client, task["id"], "x.png", b"\x89PNG\r\n\x1a\n"),
              "step_index": 1, "kind": "packet"},
    )

    assert response.status_code == 409, response.text
    assert "结构化复现" in response.text


def test_capture_uploads_and_attaches_in_one_call(client) -> None:
    """截图侧只交字节、步号、种类；它不需要知道 artifact 或挂载表的存在。"""
    task = _project_task(client)
    vulnerability = _two_step_vulnerability(client, task["id"])
    base = f"/api/v1/tasks/{task['id']}/vulnerabilities/{vulnerability['id']}/evidence"

    captured = client.post(
        f"{base}/capture",
        files={"file": ("step-02.png", b"\x89PNG\r\n\x1a\n", "image/png")},
        data={"step_index": "2", "kind": "packet", "caption": "shell 回显"},
    )

    assert captured.status_code == 201, captured.text
    payload = captured.json()
    assert payload["step_index"] == 2
    assert payload["kind"] == "packet"
    assert payload["caption"] == "shell 回显"
    listed = client.get(base).json()
    assert [item["artifact_id"] for item in listed] == [payload["artifact_id"]]
    # 图片本身能被下载——报告里 <img> 指的就是这个 URL
    assert client.get(payload["url"]).status_code == 200


def test_a_rejected_capture_leaves_no_orphan_artifact(client) -> None:
    """先校验再落盘：越界的挂载不能在上传目录里留一张没人认领的图。"""
    task = _project_task(client)
    vulnerability = _two_step_vulnerability(client, task["id"])
    before = len(client.get(f"/api/v1/tasks/{task['id']}/artifacts").json())

    response = client.post(
        f"/api/v1/tasks/{task['id']}/vulnerabilities/{vulnerability['id']}/evidence/capture",
        files={"file": ("step-09.png", b"\x89PNG\r\n\x1a\n", "image/png")},
        data={"step_index": "9", "kind": "packet"},
    )

    assert response.status_code == 409, response.text
    assert len(client.get(f"/api/v1/tasks/{task['id']}/artifacts").json()) == before


def test_re_attaching_with_a_different_kind_is_an_edit_not_a_noop(client) -> None:
    """把业务图改标成报文图会挪动闸门，必须真的落库。"""
    task = _project_task(client)
    vulnerability = _two_step_vulnerability(client, task["id"])
    artifact_id = _upload(client, task["id"], "step-01.png", b"\x89PNG\r\n\x1a\n")
    base = f"/api/v1/tasks/{task['id']}/vulnerabilities/{vulnerability['id']}/evidence"
    client.post(base, json={"artifact_id": artifact_id, "step_index": 1})

    flipped = client.post(
        base, json={"artifact_id": artifact_id, "step_index": 1, "kind": "packet"}
    )

    assert flipped.status_code == 201, flipped.text
    assert flipped.json()["kind"] == "packet"
    assert [item["kind"] for item in client.get(base).json()] == ["packet"]


def test_an_unknown_kind_is_rejected(client) -> None:
    task = _project_task(client)
    vulnerability = _vulnerability(client, task["id"])

    response = client.post(
        f"/api/v1/tasks/{task['id']}/vulnerabilities/{vulnerability['id']}/evidence",
        json={"artifact_id": "a001", "step_index": 1, "kind": "screenshot"},
    )

    assert response.status_code == 422


def test_missing_packet_steps_names_every_gap() -> None:
    from axiom.modules.reports.evidence import missing_packet_steps

    assert missing_packet_steps(0, []) == []
    assert missing_packet_steps(3, []) == [1, 2, 3]
    assert missing_packet_steps(
        3,
        [
            {"step_index": 1, "kind": "packet"},
            {"step_index": 2, "kind": "business"},
        ],
    ) == [2, 3]
    # 一张报文图覆盖一步就够，多张不重复计
    assert missing_packet_steps(
        1, [{"step_index": 1, "kind": "packet"}, {"step_index": 1, "kind": "packet"}]
    ) == []
