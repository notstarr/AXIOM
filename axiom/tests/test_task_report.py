"""The task report: one document for the whole engagement.

What is pinned here, in the order a reader meets it:

* the cover states when the report was generated — it is not a stored snapshot, so a
  re-export after a re-adjudication has to show the numbers moved;
* the body orders findings worst-first and the appendix collects what was excluded, so the
  counts on the cover always add up to what the reader can see;
* the asset/interface chapter reports what was actually called, and a task whose findings
  carry no packets says so instead of printing an empty table;
* a finding reads the same here as in its own export — the task report reuses the
  single-finding builder rather than re-describing it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from axiom.modules.reports.skeleton import EMPTY, to_task_markdown
from axiom.modules.reports.task_report import build_task_report
from axiom.modules.reproduction.schemas import Reproduction
from axiom.modules.vulnerabilities.schemas import Vulnerability


def _reproduction(*steps: tuple[str, str, str]) -> Reproduction:
    """``(host, method, path)`` per step, one exchange each."""
    return Reproduction.model_validate(
        {
            "entry_point": steps[0][1] + " " + steps[0][2] if steps else "GET /x",
            "prerequisites": ["需要低权限账号"],
            "chain_input": "",
            "chain_gain": "",
            "steps": [
                {
                    "title": f"第 {index} 步",
                    "request": f"{method} {path} HTTP/1.1\r\nHost: {host}\r\nCookie: sid=low\r\n\r\n{{}}",
                    "response": "HTTP/1.1 200 OK\r\n\r\n{}",
                    "proves": f"第 {index} 步成立",
                }
                for index, (host, method, path) in enumerate(steps, start=1)
            ],
        }
    )


def _vuln(finding_id: str, **overrides) -> Vulnerability:
    payload = {
        "id": finding_id,
        "task_id": "t239",
        "title": f"漏洞 {finding_id}",
        "type": "越权访问",
        "severity": "high",
        "description": "描述正文。",
        "prerequisites": "",
        "impact": "影响正文。",
        "remediation": "",
        "affected_target": "",
        "status": "已验证",
        "status_reason": "证据充分",
        "source": "explore",
        "reported_by": "worker-a",
        "reproduction": _reproduction(("mall.example.com", "GET", "/api/mall/products/1")),
        "created_at": "2026-10-28T00:00:00Z",
        "updated_at": "2026-10-28T00:00:00Z",
        "format_version": 3,
    }
    payload.update(overrides)
    return Vulnerability.model_validate(payload)


@dataclass
class _Task:
    id: str = "t239"
    title: str = "红雾商城"
    status: str = "completed"
    goal: str = "对红雾商城进行渗透测试"
    created_at: str = "2026-10-28 09:00:00"
    origin: object = None


GENERATED_AT = datetime(2026, 11, 2, 15, 30, tzinfo=timezone.utc)


def _report(vulnerabilities, **kwargs):
    return build_task_report(
        task=_Task(),
        vulnerabilities=vulnerabilities,
        generated_at=GENERATED_AT,
        **kwargs,
    )


def test_the_cover_records_when_the_report_was_generated() -> None:
    doc = _report([_vuln("v001")])
    cover = dict(doc.cover)
    assert cover["任务编号"] == "t239"
    assert cover["任务名称"] == "红雾商城"
    # Not a frozen snapshot: the reader has to be able to tell two exports apart.
    assert cover["报告生成时间"] == "2026-11-02 15:30 UTC"


def test_the_chapter_order_is_summary_then_surface_then_findings() -> None:
    doc = _report([_vuln("v001"), _vuln("v002")])
    keys = [section.key for section in doc.sections]
    assert keys[:2] == ["summary", "surface"]
    assert keys[2:4] == ["finding-v001", "finding-v002"]


def test_findings_are_ordered_worst_first_then_by_id() -> None:
    doc = _report(
        [
            _vuln("v003", severity="low"),
            _vuln("v002", severity="critical"),
            _vuln("v001", severity="critical"),
            _vuln("v004", severity="medium"),
        ]
    )
    order = [section.key for section in doc.sections if section.key.startswith("finding-")]
    # Two criticals keep id order so the engagement still reads in discovery order.
    assert order == ["finding-v001", "finding-v002", "finding-v004", "finding-v003"]


def test_excluded_findings_land_in_an_appendix_not_the_body() -> None:
    doc = _report(
        [
            _vuln("v001"),
            _vuln("v002", status="忽略", status_reason="环境不可复现"),
            _vuln("v003", status="重复"),
        ]
    )
    keys = [section.key for section in doc.sections]
    assert "finding-v002" not in keys
    assert "finding-v003" not in keys
    assert "appendix-findings" in keys
    appendix = next(section for section in doc.sections if section.key == "appendix-findings")
    table = next(block for block in appendix.blocks if block.type == "table")
    assert {row[0] for row in table.rows} == {"v002", "v003"}
    # The body count on the cover must match what the body actually contains.
    counts = dict(doc.counts)
    assert counts["漏洞统计"].startswith("正文 1 条")
    assert counts["未纳入正文"] == "2 条"


def test_an_unknown_status_is_kept_in_the_appendix_instead_of_vanishing() -> None:
    """A status added after this code was written must not silently drop a record.

    The schema pins today's statuses, so this is about a row that reaches the report from
    a newer deployment — the report layer must still be total over whatever it is handed.
    """

    class _Row:
        id = "v002"
        title = "漏洞 v002"
        type = "越权访问"
        severity = "high"
        status = "待复核"
        status_reason = ""
        reproduction = None

    doc = _report([_vuln("v001"), _Row()])
    keys = [section.key for section in doc.sections]
    assert "finding-v002" not in keys
    assert "appendix-findings" in keys
    appendix = next(section for section in doc.sections if section.key == "appendix-findings")
    table = next(block for block in appendix.blocks if block.type == "table")
    assert "v002" in {row[0] for row in table.rows}


def test_the_confirmed_scope_keeps_a_pending_finding_out_of_the_body() -> None:
    doc = _report([_vuln("v001"), _vuln("v002", status="待验证")])
    keys = [section.key for section in doc.sections]
    assert "finding-v001" in keys
    assert "finding-v002" not in keys
    appendix = next(section for section in doc.sections if section.key == "appendix-findings")
    table = next(block for block in appendix.blocks if block.type == "table")
    assert "v002" in {row[0] for row in table.rows}


def test_the_all_scope_promotes_the_pending_findings_into_the_body() -> None:
    doc = _report([_vuln("v001"), _vuln("v002", status="待验证")], scope="all")
    keys = [section.key for section in doc.sections]
    assert "finding-v002" in keys
    assert "appendix-findings" not in keys


def test_no_finding_is_dropped_whatever_the_scope() -> None:
    """正文加附录必须等于全部记录。

    少一条，封面上的数字就和读者能看到的列表对不上——报告就成了一个说法，不是事实。
    """
    findings = [
        _vuln("v001"),
        _vuln("v002", status="待验证"),
        _vuln("v003", status="忽略"),
        _vuln("v004", status="重复"),
    ]
    for scope in ("verified", "all"):
        doc = _report(findings, scope=scope)
        body = [item for item in doc.sections if item.key.startswith("finding-")]
        appendix = next(item for item in doc.sections if item.key == "appendix-findings")
        table = next(block for block in appendix.blocks if block.type == "table")
        assert len(body) + len(table.rows) == len(findings), scope


def test_a_status_spelling_the_dashboard_understands_is_read_the_same_way() -> None:
    """「已确认」和「已验证」在看板上是同一件事，报告里也必须是同一件事。

    状态字面量曾经散落在服务层和报告层里，各自维护一份；看板上认得的别名，报告不认，
    于是同一条记录在两个地方被分到了不同的类。
    """

    class _Row:
        id = "v002"
        title = "漏洞 v002"
        type = "越权访问"
        severity = "high"
        status = "已确认"
        status_reason = ""
        reproduction = None
        affected_target = ""
        description = "描述正文。"
        prerequisites = ""
        impact = "影响正文。"
        remediation = ""

    doc = _report([_vuln("v001"), _Row()])
    keys = [section.key for section in doc.sections]
    assert "finding-v002" in keys


def test_the_cover_does_not_claim_only_confirmed_findings_when_pending_ones_are_included() -> None:
    doc = _report([_vuln("v001"), _vuln("v002", status="待验证")], scope="all")
    summary = next(section for section in doc.sections if section.key == "summary")
    scope_text = next(block.text for block in summary.blocks if block.key == "收录范围")
    assert "已验证与待验证漏洞" in scope_text
    assert "仅已验证漏洞" not in scope_text


def test_the_cover_counts_what_the_appendix_holds() -> None:
    doc = _report([_vuln("v001"), _vuln("v002", status="忽略")])
    summary = next(section for section in doc.sections if section.key == "summary")
    scope_text = next(block.text for block in summary.blocks if block.key == "收录范围")
    assert "仅已验证漏洞" in scope_text
    assert "另有 1 条未纳入正文" in scope_text


def test_the_conclusion_does_not_call_a_pending_finding_confirmed() -> None:
    doc = _report([_vuln("v001", status="待验证")], scope="all")
    summary = next(section for section in doc.sections if section.key == "summary")
    conclusion = next(block.text for block in summary.blocks if block.key == "测试结论")
    assert "收录 1 个漏洞" in conclusion
    assert "确认" not in conclusion


def test_the_surface_covers_the_whole_task_not_only_the_findings_in_the_body() -> None:
    """被排除的记录碰过的域名仍然算在测试面里——它确实被测过。

    少了它，报告就会少说一个域名，读者以为那里没测；而这个域名的风险归属仍然只看
    正文：一条被判定为忽略的记录，不该把域名在读者面前撑成严重。
    """
    doc = _report(
        [
            _vuln("v001", reproduction=_reproduction(("mall.example.com", "GET", "/api/a"))),
            _vuln(
                "v002",
                status="忽略",
                reproduction=_reproduction(("cold.example.com", "GET", "/api/b")),
            ),
        ]
    )
    surface = next(section for section in doc.sections if section.key == "surface")
    values = {block.key: block.text for block in surface.blocks if block.type == "kv"}
    assert values["域名总数"] == "2"

    host_table = next(block for block in surface.blocks if block.type == "table")
    rows = {row[0]: row for row in host_table.rows}
    assert "cold.example.com" in rows
    # 正文里没有它的漏洞，风险栏不能凭空长出一个等级
    assert rows["cold.example.com"][3] == "0"
    assert rows["cold.example.com"][4] == EMPTY
    assert rows["mall.example.com"][3] == "1"
    assert rows["mall.example.com"][4] == "高危"


def test_the_surface_chapter_totals_the_packets() -> None:
    doc = _report(
        [
            _vuln("v001", reproduction=_reproduction(
                ("mall.example.com", "GET", "/api/mall/products/1"),
                ("mall.example.com", "GET", "/api/mall/products/2"),
            )),
            _vuln("v002", reproduction=_reproduction(
                ("desk.example.com", "POST", "/api/desk/tickets"),
            )),
        ]
    )
    surface = next(section for section in doc.sections if section.key == "surface")
    values = {block.key: block.text for block in surface.blocks if block.type == "kv"}
    assert values["域名总数"] == "2"
    # products/1 and products/2 fold into one interface: 1 + 1 ticket interface.
    assert values["接口总数"] == "2"
    assert values["报文总数"] == "3"

    host_table = next(block for block in surface.blocks if block.type == "table")
    assert host_table.headers[0] == "域名"
    assert [row[0] for row in host_table.rows] == ["mall.example.com", "desk.example.com"]
    assert [row[1] for row in host_table.rows] == ["2", "1"]


def test_each_host_gets_its_own_interface_table() -> None:
    doc = _report(
        [
            _vuln("v001", reproduction=_reproduction(("mall.example.com", "GET", "/api/a/1"))),
            _vuln("v002", reproduction=_reproduction(("desk.example.com", "GET", "/api/b"))),
        ]
    )
    surface = next(section for section in doc.sections if section.key == "surface")
    headings = [block.text for block in surface.blocks if block.type == "text"]
    assert "### mall.example.com（1 个接口）" in headings
    assert "### desk.example.com（1 个接口）" in headings


def test_a_task_without_packets_says_so_instead_of_printing_an_empty_table() -> None:
    no_repro = _vuln("v001", reproduction=None)
    doc = _report([no_repro])
    surface = next(section for section in doc.sections if section.key == "surface")
    assert all(block.type != "table" for block in surface.blocks)
    text = " ".join(block.text for block in surface.blocks if block.type == "text")
    assert "无法汇总资产与接口面" in text


def test_a_finding_keeps_the_same_chapters_it_has_in_its_own_export() -> None:
    doc = _report([_vuln("v001")])
    section = next(item for item in doc.sections if item.key == "finding-v001")
    # The single-finding builder's section titles are carried in as bold headings; the
    # packets and pictures stay inside them rather than being re-described here.
    headings = [block.text for block in section.blocks if block.type == "text"]
    assert "**描述**" in headings
    assert "**利用前提**" in headings
    assert "**测试过程**" in headings
    steps = [block for block in section.blocks if block.type == "steps"]
    assert len(steps) == 1
    assert steps[0].steps[0]["exchanges"][0]["request"].startswith("GET /api/mall/products/1")


def test_the_markdown_toc_lists_chapters_not_every_finding() -> None:
    doc = _report([_vuln(f"v{index:03d}") for index in range(1, 13)])
    markdown = to_task_markdown(doc)
    toc = markdown.split("## 目录", 1)[1].split("## ", 1)[0]
    assert "执行摘要" in toc
    assert "资产与接口" in toc
    # One line for the findings chapter, with the count — not twelve entries.
    assert "漏洞明细（12 条）" in toc
    assert toc.count("漏洞 0") == 0


def test_markdown_tables_escape_pipes_so_a_row_cannot_break() -> None:
    doc = _report(
        [
            _vuln(
                "v001",
                reproduction=_reproduction(("mall.example.com", "GET", "/api/x|y/1")),
            )
        ]
    )
    markdown = to_task_markdown(doc)
    assert "/api/x\\|y/{id}" in markdown


def test_pictures_render_under_their_step_with_the_finding_folder() -> None:
    images = {
        "v001": {
            1: [
                {
                    "step": 1,
                    "kind": "packet",
                    "path": "images/v001/step-01-packet-req.png",
                    "caption": "第 1 步回包",
                }
            ]
        }
    }
    doc = _report([_vuln("v001")], images_by_finding=images)
    markdown = to_task_markdown(doc)
    assert "![第 1 步回包](images/v001/step-01-packet-req.png)" in markdown
    # And the evidence index lists every file, so a reader can find one without scrolling.
    assert "附录 B：证据索引" in markdown
    assert "images/v001/step-01-packet-req.png" in markdown.split("附录 B")[1]


def test_no_pictures_means_no_evidence_appendix() -> None:
    doc = _report([_vuln("v001")])
    assert all(section.key != "appendix-evidence" for section in doc.sections)


def test_the_conclusion_counts_only_what_the_body_shows() -> None:
    doc = _report(
        [
            _vuln("v001", severity="critical"),
            _vuln("v002", severity="high"),
            _vuln("v003", severity="high", status="忽略"),
        ]
    )
    summary = next(section for section in doc.sections if section.key == "summary")
    conclusion = next(block.text for block in summary.blocks if block.key == "测试结论")
    assert "确认 2 个漏洞" in conclusion
    assert "严重 1 个、高危 1 个" in conclusion


def test_an_empty_task_reports_no_findings_instead_of_crashing() -> None:
    doc = _report([])
    summary = next(section for section in doc.sections if section.key == "summary")
    conclusion = next(block.text for block in summary.blocks if block.key == "测试结论")
    assert "未产出已验证漏洞" in conclusion
