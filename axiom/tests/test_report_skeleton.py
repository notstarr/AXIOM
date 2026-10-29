"""Report skeleton: chapter order, numbering and placeholders are code-owned.

The rules that matter to a delivered report:

* the chapters appear in a fixed order and a worker cannot drop one;
* steps are numbered by position, never by the worker;
* a section whose content was never required (修复建议) disappears instead of showing
  an empty heading;
* a red-team report carries the attack path but **no scores**, and a vulnerability
  report carries no attack path at all.
"""

from __future__ import annotations

from axiom.modules.redteam.schemas import RedTeamResult
from axiom.modules.reports.path import PathLink
from axiom.modules.reports.skeleton import (
    EMPTY,
    NO_REPRODUCTION,
    build_result_report,
    build_vulnerability_report,
    to_dict,
    to_markdown,
)
from axiom.modules.reproduction.schemas import Reproduction
from axiom.modules.vulnerabilities.schemas import Vulnerability


REQUEST = "POST /api/orders HTTP/1.1\r\nHost: a.example.com\r\nCookie: sid=low\r\n\r\n{}"
RESPONSE = 'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{"id":2}'


def _reproduction(*, steps: int = 1, chain_gain: str = "") -> Reproduction:
    return Reproduction.model_validate(
        {
            "entry_point": "POST /api/orders",
            "prerequisites": ["需要低权限账号", "订单接口可达"],
            "chain_input": "低权限账号会话" if chain_gain else "",
            "chain_gain": chain_gain,
            "steps": [
                {
                    "title": f"第 {index} 步",
                    "request": REQUEST,
                    "response": RESPONSE,
                    "proves": f"第 {index} 步证明越权成立",
                }
                for index in range(1, steps + 1)
            ],
        }
    )


def _vulnerability(**overrides) -> Vulnerability:
    payload = {
        "id": "v001",
        "task_id": "t001",
        "title": "订单对象级授权缺失",
        "type": "越权",
        "severity": "high",
        "description": "低权限用户可读取他人订单。",
        "prerequisites": "需要任意低权限账号。",
        "impact": "导致跨用户数据泄露。",
        "remediation": "",
        "affected_target": "a.example.com",
        "status": "待验证",
        "status_reason": "",
        "source": "explore",
        "reported_by": "worker-a",
        "reproduction": _reproduction(),
        "created_at": "2026-10-27T00:00:00Z",
        "updated_at": "2026-10-27T00:00:00Z",
        "format_version": 3,
    }
    payload.update(overrides)
    return Vulnerability.model_validate(payload)


def _result(**overrides) -> RedTeamResult:
    payload = {
        "id": "x001",
        "task_id": "t001",
        "chain_id": "ch001",
        "intent_id": "i002",
        "result_type": "pivot",
        "title": "横向到内网主机",
        "description": "拿到 192.168.0.95 的 shell。",
        "principal": "user:member",
        "tenant": "",
        "target_asset": "a.example.com",
        "evidence_refs": [],
        "evidence_artifacts": [],
        "reproduction": _reproduction(chain_gain="拿到 shell"),
        "status": "verified",
        "reported_by": "worker-a",
        "points_declared": 30,
        "points_awarded": 35,
        "review_reason": "复现完整",
        "created_at": "2026-10-27T00:00:00Z",
        "updated_at": "2026-10-27T00:00:00Z",
    }
    payload.update(overrides)
    return RedTeamResult.model_validate(payload)


def test_a_vulnerability_report_has_the_fixed_chapter_order() -> None:
    doc = build_vulnerability_report(_vulnerability())

    # 报告不再有独立的「证据」章节：图片挂在它所属的步骤下面。
    assert [section.key for section in doc.sections] == [
        "basic",
        "description",
        "process",
        "impact",
        "verdict",
    ]
    assert doc.badge == "高危"
    assert doc.status == "待验证"


def test_a_vulnerability_report_never_carries_an_attack_path() -> None:
    """Vulnerability mode does not assemble a path: the kill chain closes inside one
    Intent and the reviewer replays it."""
    doc = build_vulnerability_report(
        _vulnerability(),
        # Even if a caller passes links, the builder has no slot for them.
    )
    rendered = to_dict(doc)
    kinds = {block["type"] for section in rendered["sections"] for block in section["blocks"]}

    assert "chain" not in kinds


def test_steps_are_numbered_by_position_not_by_the_worker() -> None:
    doc = build_vulnerability_report(_vulnerability(reproduction=_reproduction(steps=3)))
    process = next(section for section in doc.sections if section.key == "process")
    steps_block = next(block for block in process.blocks if block.type == "steps")

    assert [step["index"] for step in steps_block.steps] == [1, 2, 3]
    assert [step["title"] for step in steps_block.steps] == ["第 1 步", "第 2 步", "第 3 步"]


def test_the_entry_point_and_prerequisites_come_before_the_steps() -> None:
    doc = build_vulnerability_report(_vulnerability())
    process = next(section for section in doc.sections if section.key == "process")

    assert [block.type for block in process.blocks] == ["kv", "bullets", "steps"]
    assert process.blocks[0].text == "POST /api/orders"
    assert process.blocks[1].items == ("需要低权限账号", "订单接口可达")


def test_remediation_disappears_when_empty_instead_of_showing_a_blank_heading() -> None:
    without = build_vulnerability_report(_vulnerability(remediation=""))
    with_text = build_vulnerability_report(_vulnerability(remediation="服务端校验订单归属。"))

    assert "remediation" not in [section.key for section in without.sections]
    assert "remediation" in [section.key for section in with_text.sections]


def test_a_missing_reproduction_renders_a_placeholder_not_a_crash() -> None:
    doc = build_vulnerability_report(_vulnerability(reproduction=None))
    process = next(section for section in doc.sections if section.key == "process")

    assert process.blocks[0].type == "text"
    assert process.blocks[0].text == NO_REPRODUCTION


def test_a_report_has_no_standalone_evidence_chapter() -> None:
    """Pictures belong to the step they prove, not to a chapter at the end."""
    doc = build_vulnerability_report(_vulnerability())

    assert "evidence" not in [section.key for section in doc.sections]
    assert "## 证据" not in to_markdown(doc)


def test_a_step_carries_its_own_images_into_the_markdown() -> None:
    doc = build_vulnerability_report(
        _vulnerability(reproduction=_reproduction(steps=2)),
        images_by_step={2: [{"path": "images/step-02-shell.png", "caption": "命令回显"}]},
    )
    rendered = to_markdown(doc)

    # 图片跟在第 2 步之后，且不在第 1 步里
    assert "![命令回显](images/step-02-shell.png)" in rendered
    assert rendered.index("### 步骤 2") < rendered.index("images/step-02-shell.png")
    assert rendered.index("images/step-02-shell.png") < rendered.index("## 影响")


def test_an_image_whose_step_does_not_exist_is_still_listed() -> None:
    """A picture attached to a step that no longer exists must not vanish silently."""
    doc = build_vulnerability_report(
        _vulnerability(reproduction=_reproduction(steps=1)),
        images_by_step={0: [{"path": "images/overview.png", "caption": "概览"}]},
    )
    process = next(section for section in doc.sections if section.key == "process")
    stray = next(block for block in process.blocks if block.key == "其他证据")

    assert "overview.png" in " ".join(stray.items)


def test_an_image_caption_cannot_break_the_markdown() -> None:
    """Unescaped brackets in a caption would end the alt text and break the image."""
    doc = build_vulnerability_report(
        _vulnerability(reproduction=_reproduction(steps=1)),
        images_by_step={1: [{"path": "images/a.png", "caption": "回显 [ok]"}]},
    )
    rendered = to_markdown(doc)

    assert "![回显 \\[ok\\]](images/a.png)" in rendered


def test_a_result_report_carries_the_attack_path() -> None:
    path = [
        PathLink(intent_id="i001", chain_id="ch001", title="SSRF 打到云元数据", gain="拿到 STS"),
        PathLink(intent_id="i002", chain_id="ch001", title="横向到 192.168.0.95", gain="拿到 shell", is_current=True),
    ]
    doc = build_result_report(_result(), attack_path=path)
    process = next(section for section in doc.sections if section.key == "process")
    chain = next(block for block in process.blocks if block.type == "chain")

    assert [link["intent_id"] for link in chain.links] == ["i001", "i002"]
    assert chain.links[1]["is_current"] is True


def test_a_result_report_shows_no_score_anywhere() -> None:
    """Points belong to the review verdict / scoring view, not the narrative."""
    path = [PathLink(intent_id="i001", chain_id="ch001", title="SSRF", gain="拿到 STS")]
    rendered = to_markdown(build_result_report(_result(points_awarded=35), attack_path=path))

    assert "35" not in rendered
    assert "积分" not in rendered
    assert "x001" not in rendered


def test_a_result_report_omits_capability_transitions_and_data_target() -> None:
    """Both stay in the database (the reviewer scores from them) but not in the
    delivered narrative."""
    result = _result(
        capabilities_before=["user:member"],
        capabilities_gained=["host:192.168.0.95"],
        data_target_id="customer-records-vault",
        sample_count=100,
        credential_ref="cred-1",
        data_summary="订单样本",
    )
    rendered = to_markdown(build_result_report(result))

    # The capability strings and the data-target details must not leak into the
    # narrative; the host itself legitimately appears in the worker's own description.
    assert "user:member" not in rendered
    assert "host:192.168.0.95" not in rendered
    assert "cred-1" not in rendered
    assert "订单样本" not in rendered
    assert "customer-records-vault" not in rendered


def test_a_result_without_a_path_still_renders_the_process() -> None:
    doc = build_result_report(_result(), attack_path=[])
    process = next(section for section in doc.sections if section.key == "process")

    assert [block.type for block in process.blocks] == ["kv", "bullets", "steps"]


def test_the_prerequisites_section_leads_with_chain_input() -> None:
    doc = build_result_report(_result(), attack_path=[])
    prerequisites = next(section for section in doc.sections if section.key == "prerequisites")

    assert prerequisites.blocks[0].items[0] == "低权限账号会话"
    assert "需要低权限账号" in prerequisites.blocks[0].items


def test_the_reviewer_verdict_appears_in_the_report() -> None:
    doc = build_result_report(_result(review_reason="复现完整，能力跃迁成立"))
    verdict = next(section for section in doc.sections if section.key == "verdict")

    assert any("复现完整" in (block.text or "") for block in verdict.blocks)


def test_markdown_export_does_not_repeat_the_basic_info_section() -> None:
    """The header already states type and status; a second copy is noise."""
    rendered = to_markdown(build_vulnerability_report(_vulnerability()))

    assert rendered.count("严重等级") == 0
    assert "# 订单对象级授权缺失" in rendered
    assert "## 测试过程" in rendered
    assert "### 步骤 1" in rendered


def test_markdown_export_preserves_the_packet_verbatim() -> None:
    rendered = to_markdown(build_vulnerability_report(_vulnerability()))

    assert "POST /api/orders HTTP/1.1" in rendered
    assert "Cookie: sid=low" in rendered
    assert "HTTP/1.1 200 OK" in rendered


def test_an_empty_affected_target_falls_back_to_the_placeholder() -> None:
    """affected_target is optional, so a report must still say something there."""
    doc = build_vulnerability_report(_vulnerability(affected_target=""))
    basic = next(section for section in doc.sections if section.key == "basic")
    target = next(block for block in basic.blocks if block.key == "影响目标")

    assert target.text == EMPTY
