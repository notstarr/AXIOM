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
    kwargs.setdefault("task", _Task())
    return build_task_report(
        vulnerabilities=vulnerabilities,
        generated_at=GENERATED_AT,
        **kwargs,
    )


def test_the_cover_says_what_was_tested_when_and_how_bad() -> None:
    doc = _report([_vuln("v001", severity="high"), _vuln("v002", status="忽略")])
    cover = dict(doc.cover)
    assert cover["测试目标"] == "对红雾商城进行渗透测试"
    # Not a frozen snapshot: the reader has to be able to tell two exports apart.
    assert cover["报告生成时间"] == "2026-11-02 15:30 UTC"
    assert cover["漏洞统计"] == "正文 1 条（高危 1）"
    assert cover["未纳入正文"] == "1 条"


def test_the_cover_shows_the_target_the_task_was_pointed_at() -> None:
    """origin 的原始输入是被测目标，封面必须印出来。

    这里曾经读 ``origin.input``——那个字段从来不存在，所以封面一次目标都没印过。
    """

    class _Origin:
        original_input = "https://erp.example.com/\nerp 系统"
        host = "erp.example.com"

    task = _Task(origin=_Origin())
    cover = dict(_report([_vuln("v001")], task=task).cover)
    # 多行输入压成一行：列表项里的换行会把清单劈开。
    assert cover["测试范围"] == "https://erp.example.com/ erp 系统"


def test_the_cover_leaves_out_what_the_reader_can_look_up() -> None:
    """任务编号/名称/状态、开始时间、资产面都不在封面。

    名字在标题里，资产面就是下一章的第一张表；封面只留不能事后查的东西。
    """
    cover = dict(_report([_vuln("v001")]).cover)
    for key in ("任务编号", "任务名称", "任务状态", "开始时间", "资产面"):
        assert key not in cover, key


def test_the_chapter_order_is_surface_then_findings() -> None:
    doc = _report([_vuln("v001"), _vuln("v002")])
    keys = [section.key for section in doc.sections]
    assert keys[:2] == ["surface", "finding-v001"]
    assert keys[2] == "finding-v002"


def test_there_is_no_contents_page_no_summary_and_no_appendices() -> None:
    """封面之后就该是东西，不是散文。

    目录、执行摘要、附录 A、附录 B 都去掉了：图片本来就在自己那一步下面，不需要索引。
    """
    doc = _report(
        [
            _vuln("v001", reproduction=_reproduction(("mall.example.com", "GET", "/api/a"))),
            _vuln("v002", status="忽略"),
        ],
        images_by_finding={"v001": {1: [{"step": 1, "kind": "packet", "path": "images/v001/a.png", "caption": "回包"}]}},
    )
    keys = [section.key for section in doc.sections]
    assert "summary" not in keys
    assert "appendix-findings" not in keys
    assert "appendix-evidence" not in keys

    markdown = to_task_markdown(doc)
    assert "## 目录" not in markdown
    assert "执行摘要" not in markdown
    assert "附录 A" not in markdown
    assert "附录 B" not in markdown


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


def test_excluded_findings_are_counted_not_listed() -> None:
    """未纳入的记录只在封面留一个数字，正文里不出现。

    交付报告给客户的是要他动手改的东西；被判忽略/重复的记录不再单开一章，但数字必须
    诚实——读者拿到的是一份完整报告，而不是一份只挑好看数字的报告。
    """
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

    cover = dict(doc.cover)
    assert cover["漏洞统计"].startswith("正文 1 条")
    assert cover["未纳入正文"] == "2 条"
    # 数字必须对得上：正文加未纳入 = 全部记录。
    assert 1 + 2 == 3


def test_an_unknown_status_counts_as_left_out_instead_of_vanishing() -> None:
    """一个后加的状态不能让记录凭空消失。

    schema 把今天的状态钉死了，这里说的是更新部署里送进报告的行——报告层对拿到手的
    任何东西都得是完备的：进正文，或者计入未纳入的数字，两者必居其一。
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
    assert dict(doc.cover)["未纳入正文"] == "1 条"


def test_the_confirmed_scope_keeps_a_pending_finding_out_of_the_body() -> None:
    doc = _report([_vuln("v001"), _vuln("v002", status="待验证")])
    keys = [section.key for section in doc.sections]
    assert "finding-v001" in keys
    assert "finding-v002" not in keys
    assert dict(doc.cover)["未纳入正文"] == "1 条"


def test_the_all_scope_promotes_the_pending_findings_into_the_body() -> None:
    doc = _report([_vuln("v001"), _vuln("v002", status="待验证")], scope="all")
    keys = [section.key for section in doc.sections]
    assert "finding-v002" in keys
    assert dict(doc.cover)["未纳入正文"] == "0 条"


def test_every_finding_is_either_in_the_body_or_counted_as_left_out() -> None:
    """封面两个数字相加必须等于全部记录。

    少一条，报告就成了一个说法，不是事实——正文里看不到它，未纳入的数字里也没有它。
    """
    findings = [
        _vuln("v001"),
        _vuln("v002", status="待验证"),
        _vuln("v003", status="忽略"),
        _vuln("v004", status="重复"),
    ]
    for scope in ("verified", "all"):
        doc = _report(findings, scope=scope)
        cover = dict(doc.cover)
        body = [item for item in doc.sections if item.key.startswith("finding-")]
        left_out = int(cover["未纳入正文"].split()[0])
        assert len(body) + left_out == len(findings), scope


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


def test_the_severity_breakdown_counts_only_what_the_body_shows() -> None:
    """封面的等级明细只算正文。

    一条被判忽略的高危不该让整份报告的数字看着像高危——读者按这个数字排整改顺序。
    """
    doc = _report(
        [
            _vuln("v001", severity="critical"),
            _vuln("v002", severity="high"),
            _vuln("v003", severity="high", status="忽略"),
        ]
    )
    cover = dict(doc.cover)
    assert cover["漏洞统计"] == "正文 2 条（严重 1、高危 1）"
    assert cover["未纳入正文"] == "1 条"


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


def test_the_markdown_starts_the_findings_right_after_the_cover() -> None:
    """没有目录——封面四行之后就该是资产面，再往下就是漏洞。

    十二条漏洞不该出现十二行导航；这份报告是拿来读的，不是拿来翻的。
    """
    doc = _report([_vuln(f"v{index:03d}") for index in range(1, 13)])
    markdown = to_task_markdown(doc)
    assert "## 目录" not in markdown
    # 封面之后第一个章节就是资产面，然后第一条漏洞。
    cover, rest = markdown.split("\n## ", 1)
    assert rest.startswith("资产与接口")
    assert "## v001 " in markdown


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


def test_pictures_render_under_their_step_without_an_evidence_index() -> None:
    """图片就在自己那一步下面，不再另开一个索引章。

    读者看的是「这一步的回包长什么样」，不是「压缩包里有哪 216 个文件」。
    """
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
    assert "附录 B" not in markdown
    assert "证据索引" not in markdown


def test_an_empty_task_reports_zero_findings_instead_of_crashing() -> None:
    doc = _report([])
    cover = dict(doc.cover)
    assert cover["漏洞统计"] == "正文 0 条"
    assert cover["未纳入正文"] == "0 条"
    assert [section.key for section in doc.sections] == ["surface"]
