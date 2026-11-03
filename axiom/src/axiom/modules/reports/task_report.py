"""One delivered report for a whole engagement task.

The single-finding export answers "what is this bug".  A task report answers what was
tested, when, how many findings there were and how bad, and which interfaces carry the
risk.

Chapters, in delivery order:

1. 资产与接口 — hosts reached, interfaces called (the chapter this whole module exists for)
2. 每条漏洞 — worst first, each with its own reproduced steps and screenshots

The cover carries the four things a decision-maker asks first: the target, the export
time, the finding count with its severity breakdown, and how many records were left out.
目录 / 执行摘要 / 附录 are deliberately absent — a chapter of prose before the first bug is
a page the reader skips, and the pictures already sit under their own steps.

The scope decides what the body holds: ``verified`` (the default) is the confirmed
findings only, ``all`` adds the pending ones.  Everything else is counted on the cover
and left out of the report.

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
    body is built from.  ``images_by_finding`` maps a finding id to the step-grouped
    pictures the archive wrote for it — the same shape ``build_vulnerability_report``
    takes, so the pictures land under their steps here too.

    Deliberately absent: 目录, 执行摘要 and the two appendices.  The cover carries what a
    decision-maker asks first — what was tested, when, how many findings and how bad, and
    how many records were left out — and everything after that is either the asset chapter
    or a finding.  A chapter of prose before the first bug is a page the reader skips.
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
        _surface_section(index=index, findings=body, hosts_risk=hosts_risk, body_ids=body_ids),
    ]
    sections.extend(
        _finding_section(item, images_by_step=by_finding.get(item.id, {})) for item in body
    )

    return TaskReportDoc(
        task_id=str(getattr(task, "id", "")),
        title=f"{getattr(task, 'title', '') or '渗透测试'} — 渗透测试报告",
        sections=tuple(sections),
        cover=_cover(task=task, generated_at=generated_at, body=body, appendix=appendix),
    )


# --------------------------------------------------------------------------- chapters


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
                "「关联漏洞」一列只列正文中的漏洞。"
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


# ----------------------------------------------------------------------------- cover


def _cover(
    *,
    task: Any,
    generated_at: datetime | None,
    body: list["Vulnerability"],
    appendix: list["Vulnerability"],
) -> tuple[tuple[str, str], ...]:
    """The lines a decision-maker reads before the findings.

    Deliberately few.  任务编号/名称/状态 are in the title and in the system anyway, and a
    资产面 line is just the first table of the next chapter.  What is left is what cannot
    be looked up later: what was tested, when this export was made, and what the numbers
    were at that moment.
    """
    stamp = generated_at or datetime.now(timezone.utc)
    items: list[tuple[str, str]] = []
    target = _origin_text(task)
    if target:
        items.append(("测试范围", target))
    goal = str(getattr(task, "goal", "") or "").strip()
    if goal:
        items.append(("测试目标", " ".join(goal.split())))
    items.append(("报告生成时间", stamp.strftime("%Y-%m-%d %H:%M UTC")))
    items.append(("漏洞统计", _severity_summary(body)))
    items.append(("未纳入正文", f"{len(appendix)} 条"))
    return tuple(items)


def _severity_summary(body: list["Vulnerability"]) -> str:
    """``正文 29 条（高危 8、中危 20、低危 1）`` — a count plus the worst-first breakdown."""
    severity_counts = Counter(str(item.severity) for item in body)
    parts = [
        f"{SEVERITY_LABELS.get(sev, sev)} {severity_counts[sev]}"
        for sev in ("critical", "high", "medium", "low", "info")
        if severity_counts.get(sev)
    ]
    return f"正文 {len(body)} 条" + (f"（{'、'.join(parts)}）" if parts else "")


def _origin_text(task: Any) -> str:
    """What the engagement was pointed at.

    ``Origin`` carries the raw input the operator typed (``original_input``) plus the
    parsed host/scheme/port.  This used to look for ``input``, which has never been a
    field of that model — so the cover silently printed no target at all.
    """
    origin = getattr(task, "origin", None)
    if origin is not None:
        for attribute in ("original_input", "host"):
            value = str(getattr(origin, attribute, "") or "").strip()
            if value:
                # 多行输入（URL 加一行备注）在列表项里会把清单劈开，压成一行。
                return " ".join(value.split())
    return ""


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
