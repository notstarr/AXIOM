"""The report skeleton: chapter order, numbering and placeholders owned by code.

Explore supplies *content* (a structured reproduction: real request packets, real
responses, what each step proves).  It does not supply layout.  Keeping the skeleton
here — rather than in a worker prompt — is what makes two reports look the same no
matter which worker wrote them, and what stops a worker from silently dropping a
chapter.

Two subjects share the machinery: a Vulnerability and a red-team Result.  They differ
only in which sections are relevant (a Result has no 影响/严重等级; a Vulnerability has
no 能力跃迁).  Everything below the section list is shared.

The document is produced once and rendered three ways:

``to_dict``      the console (sections in order, blocks typed)
``to_markdown``  export and notifications
``to_docx``      reserved; not implemented yet

The reviewer does not go through here at all: ``review_target.json`` carries the raw
stored row.  The reviewer reads the same data a human does, just unrendered.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from axiom.modules.reproduction.schemas import Reproduction

if TYPE_CHECKING:
    # Type-only: these modules import the report package themselves, so a runtime
    # import here would close a cycle.
    from axiom.modules.redteam.schemas import RedTeamResult
    from axiom.modules.vulnerabilities.schemas import Vulnerability


# Placeholder shown when a field exists in the skeleton but the worker left it empty.
# Rendered as italic grey in the console; kept out of the "missing" path so a report
# never silently loses a heading.
EMPTY = "未填写"
NO_REPRODUCTION = "未提供结构化复现"

BlockType = Literal["text", "kv", "bullets", "code", "steps", "verdict"]

SEVERITY_LABELS = {
    "critical": "严重",
    "high": "高危",
    "medium": "中危",
    "low": "低危",
    "info": "提示",
}

RESULT_TYPE_LABELS = {
    "permission": "权限跃迁",
    "data": "数据获取",
    "credential": "凭证获取",
    "pivot": "横向控制",
}

# What kind of picture this is. A packet shot shows the step's real response; a
# business shot shows the effect it produced. The label rides along in captions so a
# reader of the export knows which one they are looking at.
KIND_LABELS = {
    "packet": "报文图",
    "business": "业务图",
}

STATUS_LABELS = {
    "待验证": "待验证",
    "已验证": "已验证",
    "忽略": "忽略",
    "重复": "重复",
    "reported": "已上报",
    "reviewing": "审核中",
    "verified": "已验证",
    "rejected": "已驳回",
    "duplicate": "重复",
    "needs-human": "需人工",
}


@dataclass(frozen=True, slots=True)
class Block:
    type: BlockType
    # ``text`` 一行正文；``kv`` 一个键值对；``bullets`` 一串要点；``code`` 报文原文。
    text: str = ""
    key: str = ""
    items: tuple[str, ...] = ()
    steps: tuple[dict[str, Any], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"type": self.type}
        if self.text:
            payload["text"] = self.text
        if self.key:
            payload["key"] = self.key
        if self.items:
            payload["items"] = list(self.items)
        if self.steps:
            payload["steps"] = list(self.steps)
        return payload


@dataclass(frozen=True, slots=True)
class Section:
    key: str
    title: str
    blocks: tuple[Block, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "blocks": [block.as_dict() for block in self.blocks],
        }


@dataclass(frozen=True, slots=True)
class ReportDoc:
    subject_type: Literal["vulnerability", "redteam_result"]
    subject_id: str
    title: str
    badge: str
    status: str
    sections: tuple[Section, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "subject_type": self.subject_type,
            "subject_id": self.subject_id,
            "title": self.title,
            "badge": self.badge,
            "status": self.status,
            "sections": [section.as_dict() for section in self.sections],
        }


# --------------------------------------------------------------------------- builders


def build_vulnerability_report(
    vulnerability: "Vulnerability",
    *,
    images_by_step: dict[int, list[dict[str, Any]]] | None = None,
) -> ReportDoc:
    """The delivered vulnerability report.

    The report stops at what was done and what it proved. How the engagement reached
    this point is not part of it: that lives in the graph and the lineage view, and a
    reviewer adjudicating one result does not need the whole path re-narrated.
    """
    sections: list[Section] = [
        Section(
            key="basic",
            title="基本信息",
            blocks=(
                Block(type="kv", key="类型", text=vulnerability.type),
                Block(
                    type="kv",
                    key="严重等级",
                    text=SEVERITY_LABELS.get(vulnerability.severity, vulnerability.severity),
                ),
                Block(
                    type="kv",
                    key="状态",
                    text=STATUS_LABELS.get(vulnerability.status, vulnerability.status),
                ),
                Block(
                    type="kv",
                    key="影响目标",
                    text=vulnerability.affected_target or EMPTY,
                ),
            ),
        ),
        Section(
            key="description",
            title="描述",
            blocks=(Block(type="text", text=vulnerability.description or EMPTY),),
        ),
        Section(
            key="prerequisites",
            title="利用前提",
            blocks=_vulnerability_prerequisite_blocks(vulnerability),
        ),
        _process_section(vulnerability.reproduction, images_by_step=images_by_step),
        Section(
            key="impact",
            title="影响",
            blocks=(Block(type="text", text=vulnerability.impact or EMPTY),),
        ),
    ]
    # 修复建议已从 Explore 的必填项降级：审核/AI 富化补，缺就整节省略，
    # 不占一个「未填写」的空标题。
    if vulnerability.remediation.strip():
        sections.append(
            Section(
                key="remediation",
                title="修复建议",
                blocks=(Block(type="text", text=vulnerability.remediation),),
            )
        )
    sections.append(_verdict_section(
        decision=vulnerability.status,
        reason=vulnerability.status_reason,
        points=None,
    ))
    return ReportDoc(
        subject_type="vulnerability",
        subject_id=vulnerability.id,
        title=vulnerability.title,
        badge=SEVERITY_LABELS.get(vulnerability.severity, vulnerability.severity),
        status=STATUS_LABELS.get(vulnerability.status, vulnerability.status),
        sections=tuple(sections),
    )


def build_result_report(
    result: "RedTeamResult",
    *,
    images_by_step: dict[int, list[dict[str, Any]]] | None = None,
) -> ReportDoc:
    """The delivered red-team Result report.

    Deliberately absent: 能力跃迁 and 取证对象.  Both stay in the database and in the
    reviewer's ``review_target.json`` — the reviewer awards points from them — but the
    delivered report speaks in the reporter's own words plus the reproduced packets.
    """
    reproduction = result.reproduction
    sections: list[Section] = [
        Section(
            key="basic",
            title="基本信息",
            blocks=(
                Block(
                    type="kv",
                    key="类型",
                    text=RESULT_TYPE_LABELS.get(result.result_type, result.result_type),
                ),
                Block(
                    type="kv",
                    key="状态",
                    text=STATUS_LABELS.get(result.status, result.status),
                ),
            ),
        ),
        Section(
            key="description",
            title="描述",
            blocks=(Block(type="text", text=result.description or EMPTY),),
        ),
        Section(
            key="prerequisites",
            title="利用前提",
            blocks=_prerequisite_blocks(reproduction),
        ),
        _process_section(reproduction, images_by_step=images_by_step),
    ]
    sections.append(_verdict_section(
        decision=result.status,
        reason=result.review_reason,
        # 分数只进审核结论区块之外的计分视图；报告正文不展示（C1）。
        points=None,
    ))
    return ReportDoc(
        subject_type="redteam_result",
        subject_id=result.id,
        title=result.title,
        badge=RESULT_TYPE_LABELS.get(result.result_type, result.result_type),
        status=STATUS_LABELS.get(result.status, result.status),
        sections=tuple(sections),
    )


# ------------------------------------------------------------------------ shared bits


def _process_section(
    reproduction: Reproduction | None,
    *,
    images_by_step: dict[int, list[dict[str, Any]]] | None = None,
) -> Section:
    """入口点 + 有序步骤 — the part a human walks again.

    What the exploit needed before it could start is its own chapter (利用前提), above
    this one: it answers a different question, and burying it under 测试过程 made two
    reports of the same engagement read differently depending on the panel.
    """
    if reproduction is None:
        return Section(
            key="process",
            title="测试过程",
            blocks=(Block(type="text", text=NO_REPRODUCTION),),
        )
    blocks: list[Block] = [Block(type="kv", key="入口点", text=reproduction.entry_point)]
    images = images_by_step or {}
    blocks.append(
        Block(
            type="steps",
            steps=tuple(
                {
                    "index": index,
                    "title": step.title,
                    # A step may take several packets; each one is its own block so the
                    # reader (and the export) can see where one exchange ended.
                    "exchanges": [
                        {"request": exchange.request, "response": exchange.response}
                        for exchange in step.exchanges
                    ],
                    # First pair, for readers written against the single-packet shape.
                    "request": step.request,
                    "response": step.response,
                    "proves": step.proves,
                    # 该步的图片：md 导出时紧跟在响应之后，页面渲染也用它兜底
                    "images": list(images.get(index, ())),
                }
                for index, step in enumerate(reproduction.steps, start=1)
            ),
        )
    )
    # step_index 落在 1..N 之外的图（历史概览图、越界挂载）不能静默丢弃，
    # 统一收在「其他证据」里，读者仍能看到。
    stray = [item for step, items in sorted(images.items()) if step < 1 or step > len(reproduction.steps) for item in items]
    if stray:
        blocks.append(Block(type="bullets", key="其他证据", items=tuple(_image_label(item) for item in stray)))
    return Section(key="process", title="测试过程", blocks=tuple(blocks))


def _image_label(item: dict[str, Any]) -> str:
    caption = str(item.get("caption") or "").strip() or "证据"
    label = f"{KIND_LABELS.get(item.get('kind') or 'business', '证据')}·{caption}"
    path = str(item.get("path") or item.get("url") or "").strip()
    return f"{label}（{path}）" if path else label


def _vulnerability_prerequisite_blocks(vulnerability: "Vulnerability") -> tuple[Block, ...]:
    """What an attacker needs before the first packet.

    Two sources, both kept: the reporter's own sentence (``prerequisites`` — the one they
    typed on the report form) and the structured conditions the reproduction declares.
    The prose usually explains *why*, the list enumerates *what*, and dropping either
    loses something the next tester needs.
    """
    blocks: list[Block] = []
    prose = (vulnerability.prerequisites or "").strip()
    if prose:
        blocks.append(Block(type="text", text=prose))
    reproduction = vulnerability.reproduction
    conditions = list(reproduction.prerequisites) if reproduction is not None else []
    if conditions:
        blocks.append(Block(type="bullets", items=tuple(conditions)))
    if not blocks:
        return (Block(type="text", text=EMPTY),)
    return tuple(blocks)


def _prerequisite_blocks(reproduction: Reproduction | None) -> tuple[Block, ...]:
    """What this step needed before it could run.

    ``chain_input`` is the worker's one-line statement of the identity/reach it started
    with; ``prerequisites`` are the concrete conditions.  Both are local to this Intent —
    how the task got here at all is the attack path, assembled from the graph.
    """
    if reproduction is None:
        return (Block(type="text", text=EMPTY),)
    items: list[str] = []
    if reproduction.chain_input:
        items.append(reproduction.chain_input)
    items.extend(reproduction.prerequisites)
    if not items:
        return (Block(type="text", text=EMPTY),)
    return (Block(type="bullets", items=tuple(items)),)


def _verdict_section(*, decision: str, reason: str, points: int | None) -> Section:
    blocks = [
        Block(type="kv", key="结论", text=STATUS_LABELS.get(decision, decision)),
        Block(type="text", text=reason.strip() or EMPTY),
    ]
    if points is not None:
        blocks.append(Block(type="kv", key="核定积分", text=str(points)))
    return Section(key="verdict", title="审核结论", blocks=tuple(blocks))


# ---------------------------------------------------------------------------- outlets


def to_dict(doc: ReportDoc) -> dict[str, Any]:
    return doc.as_dict()


def to_markdown(doc: ReportDoc) -> str:
    lines = [f"# {doc.title}", "", f"- 类型：{doc.badge}", f"- 状态：{doc.status}", ""]
    for section in doc.sections:
        if section.key == "basic":
            # Already in the header above; repeating it as a section would be noise.
            continue
        lines.extend([f"## {section.title}", ""])
        for block in section.blocks:
            lines.extend(_render_block(block))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_block(block: Block) -> list[str]:
    if block.type == "text":
        return [block.text, ""]
    if block.type == "kv":
        return [f"**{block.key}**：{block.text}", ""]
    if block.type == "bullets":
        rendered = [f"- {item}" for item in block.items] or [f"- {EMPTY}"]
        return [*rendered, ""]
    if block.type == "code":
        return ["```http", block.text, "```", ""]
    if block.type == "verdict":
        return [_verdict_line(block), ""]
    if block.type == "steps":
        lines = []
        for step in block.steps:
            lines.append(f"### 步骤 {step['index']} — {step['title']}")
            lines.append("")
            # 「描述」而不是「证明」：这一步做了什么，读者自己判断它证明了什么。
            lines.append(f"描述：{step['proves']}")
            lines.append("")
            for position, exchange in enumerate(
                step.get("exchanges") or [{"request": step["request"], "response": step["response"]}],
                start=1,
            ):
                if position > 1:
                    # 一组报文一个分节：两组挤在同一块里，读者分不清哪行响应属于哪个请求。
                    lines.append(f"报文 {position}：")
                    lines.append("")
                lines.append("```http")
                lines.append(exchange["request"])
                lines.append("```")
                lines.append("")
                lines.append("响应：")
                lines.append("")
                lines.append("```http")
                lines.append(exchange["response"])
                lines.append("```")
                lines.append("")
            # 证据图紧跟该步的响应：读者看到的就是这一步发生了什么，
            # 不必翻到文末再对号入座。
            for image in step.get("images") or ():
                lines.append(_image_markdown(image, step["index"]))
                lines.append("")
        return lines
    return []


def _image_markdown(image: dict[str, Any], step_index: int) -> str:
    """One markdown image line.

    The caption is user text, so brackets are escaped — an unescaped ``]`` would end the
    alt text early and break the image for every renderer.
    """
    caption = str(image.get("caption") or "").strip()
    if not caption:
        # An uncaptioned picture still says which kind it is: a bare "步骤 2 证据" on a
        # business shot reads as if it proved the step.
        kind = KIND_LABELS.get(str(image.get("kind") or "business"), "证据")
        caption = f"步骤 {step_index} {kind}"
    for char in ("\\", "[", "]"):
        caption = caption.replace(char, "\\" + char)
    return f"![{caption}]({image.get('path') or ''})"


def _verdict_line(block: Block) -> str:
    return block.text


def to_docx(doc: ReportDoc) -> bytes:
    """Reserved outlet.

    Deliberately unimplemented: the deliverable this round is the in-console report and
    the Markdown export.  A docx renderer would need a template and font handling that
    are not part of this change, and a half-working one would silently produce
    unformatted output.
    """
    raise NotImplementedError("docx 报告尚未实现；当前交付物是页面报告与 Markdown 导出")
