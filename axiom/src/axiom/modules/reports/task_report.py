"""One delivered report for a whole engagement task.

The single-finding export answers "what is this bug".  A task report has to answer four
more questions before the reader gets to the bugs, and they are the ones a client asks
first: what was in scope, how much surface was touched, what was found in total, and which
interfaces carry the risk.

Chapters, in delivery order:

1. 执行摘要 — scope, window, generated-at, counts by severity and type
2. 资产与接口 — hosts reached, interfaces called (the chapter this whole module exists for)
3. 漏洞明细 — every finding, worst first, each with its own reproduced steps
4. 附录：未纳入正文的漏洞 — everything the scope left out (pending under the confirmed
   scope, ignored, duplicate), so the report is complete but the body stays about what
   matters
5. 附录：证据索引 — every screenshot, by finding and step

The scope decides what the body holds: ``verified`` (the default) is the confirmed
findings only, ``all`` adds the pending ones.  Everything else lands in appendix A, so
the counts on the cover always add up to the records the reader can see.

Two things this module deliberately does **not** do:

* It does not re-render a finding.  Each entry reuses :func:`build_vulnerability_report`
  so a finding reads identically here and in its own export.  If the two ever disagree,
  one of them is wrong and the reader cannot tell which.
* It does not hide empty sections.  A task with no reproduction still gets 测试过程, showing
  the "未提供复现" placeholder — silence would read as "nothing to see", which is a
  different and wrong claim.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Iterable

from sqlalchemy import Connection

from axiom.modules.reports.interfaces import SEVERITY_RANK, InterfaceIndex, build_index
from axiom.modules.reports.skeleton import (
    EMPTY,
    SEVERITY_LABELS,
    STATUS_LABELS,
    Block,
    ReportDoc,
    Section,
    TaskReportDoc,
    build_vulnerability_report,
)
from axiom.modules.vulnerabilities.status import PENDING, VERIFIED, status_key

if TYPE_CHECKING:
    from axiom.modules.vulnerabilities.schemas import Vulnerability


# Worst first: the reader stops reading when the severity drops, so order is the writing.
# SEVERITY_RANK (same numbers, used for picking a host's worst finding) lives in interfaces.py
# so the index and the chapter that prints it can never disagree.
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}

# What a delivered body includes, per scope.  A confirmed finding is what the client is
# being asked to fix; a pending one exists but nobody has confirmed it yet.  Everything
# else — ignored, duplicate, or a state this code has never seen — is still a record, and
# it goes in the appendix: a record that vanishes silently makes the cover's counts
# disagree with the list.
BODY_STATUSES_BY_SCOPE: dict[str, frozenset[str]] = {
    "verified": frozenset({VERIFIED}),
    "all": frozenset({VERIFIED, PENDING}),
}

# Interfaces listed per host before the table is truncated.  A host with 200 interfaces is
# a directory listing, not a finding, and printing all of them buries the ones that matter.
MAX_INTERFACES_PER_HOST = 40
# Rows in the host summary table.  Aggregate traffic lands on a handful of hosts; the tail
# is one-call noise.
MAX_HOST_ROWS = 60


def split_by_scope(
    vulnerabilities: Iterable["Vulnerability"], scope: str = "verified"
) -> tuple[list["Vulnerability"], list["Vulnerability"]]:
    """Split findings into ``(body, appendix)`` for one scope.

    Every finding lands in exactly one of the two.  A record that is neither confirmed
    nor pending is not something the client is being asked to act on, but it is still a
    record — ignored, duplicate, or a status nobody anticipated — and it belongs in the
    appendix so the report stays complete.

    Both the builder and the export service ask this function, so the pictures that go
    into the archive and the findings that go into the body are decided by the same rule.
    """
    body_keys = BODY_STATUSES_BY_SCOPE.get(scope, BODY_STATUSES_BY_SCOPE["verified"])
    body: list["Vulnerability"] = []
    appendix: list["Vulnerability"] = []
    for item in vulnerabilities:
        if status_key(_status_of(item)) in body_keys:
            body.append(item)
        else:
            appendix.append(item)
    return body, appendix


def build_task_report(
    *,
    task: Any,
    vulnerabilities: Iterable["Vulnerability"],
    images_by_finding: dict[str, dict[int, list[dict[str, Any]]]] | None = None,
    generated_at: datetime | None = None,
    scope: str = "verified",
) -> TaskReportDoc:
    """Assemble the task report.

    ``vulnerabilities`` is every finding the task has; ``scope`` decides which of them the
    body is built from and where the rest go.  ``images_by_finding`` maps a finding id to
    the step-grouped pictures the archive wrote for it — the same shape
    ``build_vulnerability_report`` takes, so the pictures land under their steps here too.
    """
    findings = list(vulnerabilities)
    body, appendix = split_by_scope(findings, scope)
    body.sort(key=lambda item: (SEVERITY_ORDER.get(str(item.severity), 9), _finding_sort_key(item)))

    # 资产面统计的是整个任务：被排除的记录碰过的域名同样被测过，少算一个域名，读者
    # 就会以为那里没测。风险归属另算——只看正文，一条被判定忽略的记录不该把域名在
    # 读者面前撑成严重。
    index = build_index(findings, severity_rank=SEVERITY_RANK)
    hosts_risk, body_ids = _body_attribution(index, body)
    by_finding = images_by_finding or {}

    sections: list[Section] = [
        _summary_section(task=task, body=body, appendix=appendix, index=index, scope=scope),
        _surface_section(index=index, findings=body, hosts_risk=hosts_risk, body_ids=body_ids),
    ]
    sections.extend(
        _finding_section(item, images_by_step=by_finding.get(item.id, {})) for item in body
    )
    if appendix:
        sections.append(_appendix_section(appendix))
    # 证据索引与「步骤下的图」读同一份数据：分开传两份，迟早一份更新一份没更新。
    evidence = _evidence_section(body, images_by_finding=by_finding)
    if evidence is not None:
        sections.append(evidence)

    return TaskReportDoc(
        task_id=str(getattr(task, "id", "")),
        title=f"{getattr(task, 'title', '') or '渗透测试'} — 渗透测试报告",
        sections=tuple(sections),
        cover=_cover(task=task, generated_at=generated_at),
        counts=_counts(body=body, appendix=appendix, index=index),
    )


# --------------------------------------------------------------------------- chapters


def _summary_section(
    *,
    task: Any,
    body: list["Vulnerability"],
    appendix: list["Vulnerability"],
    index: InterfaceIndex,
    scope: str,
) -> Section:
    severity_counts = Counter(str(item.severity) for item in body)
    severity_items = tuple(
        f"{SEVERITY_LABELS.get(sev, sev)}：{severity_counts[sev]}"
        for sev in ("critical", "high", "medium", "low", "info")
        if severity_counts.get(sev)
    )
    type_counts = Counter(str(item.type).strip() for item in body if str(item.type).strip())
    type_items = tuple(f"{name}：{count}" for name, count in type_counts.most_common(12))

    blocks: list[Block] = []
    goal = str(getattr(task, "goal", "") or "").strip()
    if goal:
        blocks.append(Block(type="kv", key="测试目标", text=goal))
    blocks.append(
        Block(
            type="kv",
            key="收录范围",
            text=(
                "仅已验证漏洞"
                if scope == "verified"
                else "已验证与待验证漏洞"
            )
            + (f"；另有 {len(appendix)} 条未纳入正文，列于附录" if appendix else ""),
        )
    )
    blocks.append(
        Block(
            type="kv",
            key="测试结论",
            text=_conclusion(body=body, index=index, scope=scope),
        )
    )
    if severity_items:
        blocks.append(Block(type="bullets", key="按严重等级", items=severity_items))
    if type_items:
        blocks.append(Block(type="bullets", key="按漏洞类型", items=type_items))
    return Section(key="summary", title="执行摘要", blocks=tuple(blocks))


def _conclusion(*, body: list["Vulnerability"], index: InterfaceIndex, scope: str = "verified") -> str:
    """One sentence a decision-maker can quote.

    Written from the counts rather than from a template with blanks: it has to be true for
    a two-finding task and for a fifty-finding one, and it must not claim a risk level the
    evidence does not support.  The verb follows the scope — under ``verified`` every
    finding in the body has been confirmed, and calling a pending one "confirmed" would
    overstate what the report actually proves.
    """
    critical = sum(1 for item in body if str(item.severity) == "critical")
    high = sum(1 for item in body if str(item.severity) == "high")
    if not body:
        return "本次测试未产出漏洞。" if scope == "all" else "本次测试未产出已验证漏洞。"
    verb = "确认" if scope == "verified" else "收录"
    parts = [f"本次测试在 {index.host_count} 个域名上{verb} {len(body)} 个漏洞"]
    if critical or high:
        parts.append(f"其中严重 {critical} 个、高危 {high} 个")
    if index.interface_count:
        parts.append(f"涉及 {index.interface_count} 个接口")
    worst = "严重" if critical else ("高危" if high else "中低危")
    return "，".join(parts) + f"；整体风险判定为{worst}，建议按各条修复建议逐项整改。"


def _body_attribution(
    index: InterfaceIndex, body: list["Vulnerability"]
) -> tuple[dict[str, tuple[set[str], str, str]], set[str]]:
    """What the body puts on each host, and which finding ids it contains.

    Returns ``(per host, all body ids)``.  A host entry is ``(finding ids, worst
    severity, first finding id)`` — the three things the domain table prints.

    The surface itself is the whole task's; the attribution is the body's.  A finding
    that was dismissed must not make a host look critical to the reader, and a host the
    body has nothing to say about still belongs in the table — it was tested.
    """
    body_ids = {str(item.id) for item in body}
    hosts: dict[str, tuple[set[str], str, str]] = {}
    for item in body:
        severity = str(item.severity or "")
        finding_id = str(item.id)
        for row in index.interfaces_for(finding_id):
            if not row.host:
                continue
            ids, worst, first = hosts.get(row.host, (set(), "", ""))
            ids.add(finding_id)
            if severity and SEVERITY_RANK.get(severity, 0) > SEVERITY_RANK.get(worst, 0):
                worst = severity
            if not first:
                first = finding_id
            hosts[row.host] = (ids, worst, first)
    return hosts, body_ids


def _surface_section(
    *,
    index: InterfaceIndex,
    findings: list["Vulnerability"],
    hosts_risk: dict[str, tuple[set[str], str, str]],
    body_ids: set[str],
) -> Section:
    """附资产与接口 —— the chapter the single-finding export never had."""
    if index.host_count == 0 and index.interface_count == 0:
        return Section(
            key="surface",
            title="资产与接口",
            blocks=(
                Block(
                    type="text",
                    text="本次测试的漏洞记录中没有可解析的报文，无法汇总资产与接口面。",
                ),
            ),
        )

    blocks: list[Block] = [
        Block(type="kv", key="域名总数", text=str(index.host_count)),
        Block(type="kv", key="接口总数", text=str(index.interface_count)),
        Block(type="kv", key="报文总数", text=str(index.call_count)),
        Block(
            type="text",
            text=(
                "本章统计本次任务复现出的全部报文，因此包含未纳入正文的漏洞碰过的接口；"
                "「关联漏洞」一列只列正文中的漏洞，其余记录见附录 A。"
            ),
        ),
        Block(
            type="table",
            headers=("域名", "报文数", "接口数", "关联漏洞数", "最高等级", "首见漏洞"),
            rows=tuple(
                (
                    row.host,
                    str(row.calls),
                    str(len(row.interfaces)),
                    str(len(hosts_risk.get(row.host, (set(), "", ""))[0])),
                    SEVERITY_LABELS.get(hosts_risk.get(row.host, (set(), "", ""))[1], EMPTY)
                    or EMPTY,
                    hosts_risk.get(row.host, (set(), "", ""))[2] or EMPTY,
                )
                for row in index.hosts[:MAX_HOST_ROWS]
            ),
        ),
    ]
    if len(index.hosts) > MAX_HOST_ROWS:
        blocks.append(
            Block(
                type="text",
                text=f"（按报文数取前 {MAX_HOST_ROWS} 个域名，另有 {len(index.hosts) - MAX_HOST_ROWS} 个未列出）",
            )
        )

    by_host: dict[str, list[Any]] = {}
    for row in index.interfaces:
        by_host.setdefault(row.host, []).append(row)
    for host, rows in by_host.items():
        shown = rows[:MAX_INTERFACES_PER_HOST]
        interface_rows = tuple(
            (
                row.method,
                row.path,
                str(row.calls),
                row.status_text or EMPTY,
                "、".join(sorted(row.finding_ids & body_ids)),
            )
            for row in shown
        )
        blocks.append(Block(type="text", text=f"### {host}（{len(rows)} 个接口）"))
        blocks.append(
            Block(
                type="table",
                headers=("方法", "路径", "次数", "状态码", "关联漏洞"),
                rows=interface_rows,
            )
        )
        if len(rows) > MAX_INTERFACES_PER_HOST:
            blocks.append(
                Block(
                    type="text",
                    text=f"（仅列前 {MAX_INTERFACES_PER_HOST} 个接口，另有 {len(rows) - MAX_INTERFACES_PER_HOST} 个未列出）",
                )
            )

    if index.unattributed:
        blocks.append(Block(type="text", text="### 未能归属域名的报文"))
        blocks.append(
            Block(
                type="table",
                headers=("方法", "路径", "次数", "状态码", "关联漏洞"),
                rows=tuple(
                    (
                        row.method,
                        row.path,
                        str(row.calls),
                        row.status_text or EMPTY,
                        "、".join(sorted(row.finding_ids & body_ids)),
                    )
                    for row in index.unattributed[:MAX_INTERFACES_PER_HOST]
                ),
            )
        )
    return Section(key="surface", title="资产与接口", blocks=tuple(blocks))


def _finding_section(
    vulnerability: "Vulnerability",
    *,
    images_by_step: dict[int, list[dict[str, Any]]],
) -> Section:
    """One finding, rendered by the same builder its standalone export uses.

    The standalone document's sections are flattened into this one as blocks, so a step
    keeps its packets and pictures and there is exactly one implementation of "how a
    finding is described".
    """
    doc: ReportDoc = build_vulnerability_report(vulnerability, images_by_step=images_by_step)
    blocks: list[Block] = [
        Block(
            type="kv",
            key="编号",
            text=f"{vulnerability.id} · {SEVERITY_LABELS.get(str(vulnerability.severity), vulnerability.severity)}"
            f" · {STATUS_LABELS.get(str(vulnerability.status), vulnerability.status)}",
        ),
    ]
    for section in doc.sections:
        if section.key == "basic":
            continue
        blocks.append(Block(type="text", text=f"**{section.title}**"))
        blocks.extend(section.blocks)
    return Section(
        key=f"finding-{vulnerability.id}",
        title=f"{vulnerability.id} {vulnerability.title}",
        blocks=tuple(blocks),
    )


def _appendix_section(appendix: list["Vulnerability"]) -> Section:
    rows = tuple(
        (
            item.id,
            str(item.title),
            str(item.type),
            SEVERITY_LABELS.get(str(item.severity), str(item.severity)),
            STATUS_LABELS.get(str(item.status), str(item.status)),
            (str(item.status_reason or "").strip() or EMPTY)[:80],
        )
        for item in appendix
    )
    return Section(
        key="appendix-findings",
        title="附录 A：未纳入正文的漏洞",
        blocks=(
            Block(
                type="text",
                text="以下记录未纳入正文（待验证、已忽略或判为重复），不计入执行摘要中的统计，列出以保证报告完整。",
            ),
            Block(
                type="table",
                headers=("编号", "标题", "类型", "等级", "状态", "理由"),
                rows=rows,
            ),
        ),
    )


def _evidence_section(
    body: list["Vulnerability"],
    *,
    images_by_finding: dict[str, dict[int, list[dict[str, Any]]]],
) -> Section | None:
    """附录 B：every picture in the package, so a reader can find the file they need."""
    rows: list[tuple[str, ...]] = []
    for item in body:
        for group in images_by_finding.get(item.id, {}).values():
            for picture in group:
                rows.append(
                    (
                        item.id,
                        str(picture.get("step") or 0),
                        str(picture.get("kind") or ""),
                        str(picture.get("caption") or EMPTY),
                        str(picture.get("path") or ""),
                    )
                )
    if not rows:
        return None
    rows.sort(key=lambda row: (row[0], int(row[1]) if row[1].isdigit() else 0))
    return Section(
        key="appendix-evidence",
        title="附录 B：证据索引",
        blocks=(
            Block(
                type="text",
                text="图片文件与本报告同在压缩包内，路径相对于报告根目录。",
            ),
            Block(
                type="table",
                headers=("漏洞", "步骤", "类型", "说明", "文件"),
                rows=tuple(rows),
            ),
        ),
    )


# ----------------------------------------------------------------------------- cover


def _cover(*, task: Any, generated_at: datetime | None) -> tuple[tuple[str, str], ...]:
    stamp = generated_at or datetime.now(timezone.utc)
    created = getattr(task, "created_at", None)
    items = [
        ("任务编号", str(getattr(task, "id", "") or EMPTY)),
        ("任务名称", str(getattr(task, "title", "") or EMPTY)),
        ("任务状态", str(getattr(task, "status", "") or EMPTY)),
    ]
    input_text = _origin_text(task)
    if input_text:
        items.append(("测试范围", input_text))
    if created:
        items.append(("开始时间", str(created)[:19]))
    items.append(("报告生成时间", stamp.strftime("%Y-%m-%d %H:%M UTC")))
    return tuple(items)


def _origin_text(task: Any) -> str:
    origin = getattr(task, "origin", None)
    if origin is None:
        return ""
    for attribute in ("input", "host", "url"):
        value = str(getattr(origin, attribute, "") or "").strip()
        if value:
            return value
    return ""


def _counts(
    *,
    body: list["Vulnerability"],
    appendix: list["Vulnerability"],
    index: InterfaceIndex,
) -> tuple[tuple[str, str], ...]:
    severity_counts = Counter(str(item.severity) for item in body)
    parts = [
        f"{SEVERITY_LABELS.get(sev, sev)} {severity_counts[sev]}"
        for sev in ("critical", "high", "medium", "low", "info")
        if severity_counts.get(sev)
    ]
    items = [
        ("漏洞统计", f"正文 {len(body)} 条" + (f"（{'、'.join(parts)}）" if parts else "")),
    ]
    if appendix:
        items.append(("未纳入正文", f"{len(appendix)} 条"))
    items.append(("资产面", f"{index.host_count} 个域名 / {index.interface_count} 个接口"))
    return tuple(items)


def _status_of(vulnerability: "Vulnerability") -> str:
    return str(getattr(vulnerability, "status", "") or "")


def _finding_sort_key(vulnerability: "Vulnerability") -> str:
    # Within one severity, oldest id first: v001 before v050 reads as the order the
    # engagement found them, which is how a reader reconstructs the path.
    return str(getattr(vulnerability, "id", "") or "")


__all__ = [
    "BODY_STATUSES_BY_SCOPE",
    "MAX_HOST_ROWS",
    "MAX_INTERFACES_PER_HOST",
    "SEVERITY_ORDER",
    "SEVERITY_RANK",
    "build_task_report",
    "split_by_scope",
]
