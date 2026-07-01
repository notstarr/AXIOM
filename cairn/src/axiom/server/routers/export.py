from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from datetime import datetime
import re
import yaml

from axiom.server.db import get_conn
from axiom.server.services import expire_reason_leases, expire_workers, get_project_or_404, build_vulns

router = APIRouter(tags=["export"])


def format_export_timestamp(value: str | None) -> str | None:
    if not value:
        return value
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _load_project_data(conn, project_id: str):
    expire_workers(conn, project_id)
    expire_reason_leases(conn, project_id)
    proj = get_project_or_404(conn, project_id)

    facts = conn.execute(
        "SELECT id, description, archived FROM facts WHERE project_id = ?", (project_id,)
    ).fetchall()
    hints = conn.execute(
        "SELECT content, creator, created_at FROM hints WHERE project_id = ? ORDER BY created_at",
        (project_id,),
    ).fetchall()
    intents = conn.execute(
        "SELECT * FROM intents WHERE project_id = ? ORDER BY created_at",
        (project_id,),
    ).fetchall()

    sources_by_intent = {}
    for i in intents:
        rows = conn.execute(
            "SELECT fact_id FROM intent_sources WHERE intent_id = ? AND project_id = ? ORDER BY rowid",
            (i["id"], project_id),
        ).fetchall()
        sources_by_intent[i["id"]] = [r["fact_id"] for r in rows]

    return proj, facts, hints, intents, sources_by_intent


def _summarize_fact(
    fact,
    max_chars: int = 50,
    preserve_fact_ids: set[str] | None = None,
) -> str:
    """Truncate fact body, preserve evidence file paths in full."""
    try:
        desc = fact["description"]
        fact_id = fact["id"]
    except (KeyError, TypeError):
        return ""
    # origin/goal and bootstrap-produced facts are never truncated
    if fact_id in ("origin", "goal") or (
        preserve_fact_ids is not None and fact_id in preserve_fact_ids
    ):
        return desc
    text = desc.replace("\n", " ").strip()
    # Extract evidence paths
    path_pattern = r"(?:\{ai_dir\}|/tmp/axiom-files)/\S+\.md"
    paths = re.findall(path_pattern, text)
    # Remove paths and trailing connector text from body
    body = re.sub(r"\s*(?:详细测试记录见|证据文件见|详见)\s*", "", re.sub(path_pattern, "", text)).strip()
    if len(body) > max_chars:
        body = body[:max_chars - 3].rstrip() + "..."
    if paths:
        body = f"{body} 证据: {', '.join(paths)}"
    return body


def _summarize_intent_description(text: str, max_chars: int = 80) -> str:
    compact = (text or "").replace("\n", " ").strip()
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 3].rstrip() + "..."


def _export_yaml(conn, project_id: str) -> str:
    proj, facts, hints, intents, sources_by_intent = _load_project_data(conn, project_id)
    bootstrap_fact_ids = {
        i["to_fact_id"]
        for i in intents
        if i["creator"] == "dispatcher.bootstrap" and i["to_fact_id"]
    }

    origin_desc = ""
    goal_desc = ""
    for f in facts:
        if f["id"] == "origin":
            origin_desc = f["description"]
        elif f["id"] == "goal":
            goal_desc = f["description"]

    data: dict = {
        "project": {
            "id": proj["id"],
            "title": proj["title"],
            "origin": origin_desc,
            "goal": goal_desc,
            "bootstrap_enabled": bool(proj["bootstrap_enabled"]),
        }
    }

    if hints:
        data["hints"] = [
            {
                "content": h["content"],
                "creator": h["creator"],
                "created_at": format_export_timestamp(h["created_at"]),
            }
            for h in hints
        ]

    data["facts"] = [
        {
            "id": f["id"],
            "description": _summarize_fact(f, preserve_fact_ids=bootstrap_fact_ids),
            "lookup_path": f"/projects/{project_id}?fact_id={f['id']}",
        }
        for f in facts
        if not f["archived"]
    ]

    # Build set of archived fact IDs for filtering intent sources
    archived_fact_ids = {f["id"] for f in facts if f["archived"]}

    intent_list = []
    for i in intents:
        if i["archived"]:
            continue
        summarized_description = _summarize_intent_description(i["description"])
        entry: dict = {
            "id": i["id"],
            "from": [fid for fid in sources_by_intent.get(i["id"], []) if fid not in archived_fact_ids],
            "to": i["to_fact_id"],
            "description": summarized_description,
            "status": "concluded" if i["concluded_at"] else "open",
            "lookup_path": f"/projects/{project_id}?intent_id={i['id']}",
        }
        intent_list.append(entry)

    if intent_list:
        data["intents"] = intent_list

    return yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False)


def _export_timeline(conn, project_id: str) -> str:
    proj, facts, hints, intents, sources_by_intent = _load_project_data(conn, project_id)
    bootstrap_fact_ids = {
        i["to_fact_id"]
        for i in intents
        if i["creator"] == "dispatcher.bootstrap" and i["to_fact_id"]
    }

    facts_by_id = {
        f["id"]: _summarize_fact(f, preserve_fact_ids=bootstrap_fact_ids)
        for f in facts
    }

    events: list[tuple[str, int, str]] = []  # (timestamp, order, text)
    order = 0

    origin_desc = facts_by_id.get("origin", "")
    goal_desc = facts_by_id.get("goal", "")
    ts = format_export_timestamp(proj["created_at"]) or ""
    block = f"[{ts}] PROJECT CREATED\n  origin: {origin_desc}\n  goal: {goal_desc}"
    events.append((proj["created_at"] or "", order, block))
    order += 1

    for h in hints:
        ts = format_export_timestamp(h["created_at"]) or ""
        block = f"[{ts}] HINT by {h['creator']}\n  {h['content']}"
        events.append((h["created_at"] or "", order, block))
        order += 1

    for i in intents:
        src = sources_by_intent.get(i["id"], [])
        from_str = ", ".join(src)

        ts = format_export_timestamp(i["created_at"]) or ""
        meta = f"  from: {from_str}"
        if i["worker"] and not i["concluded_at"]:
            meta += f"\n  worker: {i['worker']} (in progress)"
        block = f"[{ts}] INTENT DECLARED {i['id']} by {i['creator']}\n{meta}\n  {i['description']}"
        events.append((i["created_at"] or "", order, block))
        order += 1

        if not i["concluded_at"] or not i["to_fact_id"]:
            continue

        ts = format_export_timestamp(i["concluded_at"]) or ""
        actor = i["worker"] or i["creator"]

        if i["to_fact_id"] == "goal":
            block = f"[{ts}] PROJECT COMPLETED by {actor}\n  via: {i['id']} from {from_str}"
        else:
            fact_desc = facts_by_id.get(i["to_fact_id"], "")
            block = f"[{ts}] INTENT CONCLUDED {i['id']} by {actor}\n  from: {from_str}\n  produced: {i['to_fact_id']}\n  {fact_desc}"

        events.append((i["concluded_at"] or "", order, block))
        order += 1

    events.sort(key=lambda e: (e[0], e[1]))

    return "\n\n".join(e[2] for e in events) + "\n"


_SEVERITY_EMOJI = {
    "critical": "🔴",
    "high": "🟠",
    "medium": "🟡",
    "low": "🔵",
    "info": "⚪",
}

_STATUS_LABEL = {
    "待确认": "待确认",
    "已验证": "已验证",
    "忽略": "忽略",
    "重复": "重复",
    # backward compatibility for historical records
    "pending": "待确认",
    "confirmed": "已验证",
    "ignored": "忽略",
    "rejected": "重复",
}


def _format_vuln_md(v, project_title: str = "") -> list[str]:
    emoji = _SEVERITY_EMOJI.get(v.severity, "")
    status_label = _STATUS_LABEL.get(v.status, v.status)
    lines: list[str] = []
    lines.append(f"# {v.title}")
    lines.append("")
    lines.append(f"- **漏洞 ID**: `{v.id}`")
    if project_title:
        lines.append(f"- **所属项目**: {project_title}")
    lines.append(f"- **类型**: {v.type or '未分类'}")
    lines.append(f"- **严重程度**: {emoji} {v.severity}")
    lines.append(f"- **状态**: {status_label}")
    lines.append(f"- **影响范围**: {v.affected_scope or '—'}")
    lines.append(f"- **上报来源**: {v.source}")
    lines.append(f"- **上报者**: {v.reported_by}")
    lines.append(f"- **发现时间**: {format_export_timestamp(v.created_at) or '—'}")
    lines.append("")

    lines.append("## 描述")
    lines.append("")
    lines.append(v.description or "—")
    lines.append("")

    if v.prerequisites:
        lines.append("## 利用前提")
        lines.append("")
        lines.append(v.prerequisites)
        lines.append("")

    lines.append("## 影响")
    lines.append("")
    lines.append(v.impact or "—")
    lines.append("")

    if v.evidence:
        lines.append("## 证据")
        lines.append("")
        for ev in v.evidence:
            lines.append(f"```\n{ev}\n```")
            lines.append("")

    if v.reproduction_steps:
        lines.append("## 复现步骤")
        lines.append("")
        for idx, step in enumerate(v.reproduction_steps, 1):
            lines.append(f"{idx}. {step}")
        lines.append("")

    if v.recommendation:
        lines.append("## 修复建议")
        lines.append("")
        lines.append(v.recommendation)
        lines.append("")

    return lines


def _export_vulns_md(conn, project_id: str) -> str:
    proj = get_project_or_404(conn, project_id)
    vulns = build_vulns(conn, project_id)

    lines: list[str] = []
    lines.append(f"# 漏洞报告 — {proj['title']}")
    lines.append("")
    lines.append(f"- **项目 ID**: `{project_id}`")
    lines.append(f"- **漏洞数量**: {len(vulns)}")
    lines.append(f"- **生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")
    lines.append("---")
    lines.append("")

    if not vulns:
        lines.append("*暂无漏洞记录*")
        return "\n".join(lines)

    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    sorted_vulns = sorted(vulns, key=lambda v: severity_order.get(v.severity, 99))

    summary_lines = ["## 概览", ""]
    summary_lines.append("| # | ID | 严重程度 | 状态 | 标题 |")
    summary_lines.append("|---|-----|---------|------|------|")
    for i, v in enumerate(sorted_vulns, 1):
        emoji = _SEVERITY_EMOJI.get(v.severity, "")
        status_label = _STATUS_LABEL.get(v.status, v.status)
        summary_lines.append(f"| {i} | {v.id} | {emoji} {v.severity} | {status_label} | {v.title} |")
    summary_lines.append("")
    summary_lines.append("---")
    summary_lines.append("")
    lines.extend(summary_lines)

    for i, v in enumerate(sorted_vulns, 1):
        lines.append(f"## {i}. {v.title}")
        lines.append("")
        lines.extend(_format_vuln_md(v, project_title=proj["title"]))
        lines.append("---")
        lines.append("")

    return "\n".join(lines)


def _export_single_vuln_md(conn, project_id: str, vuln_id: str) -> str:
    proj = get_project_or_404(conn, project_id)
    vulns = build_vulns(conn, project_id)
    vuln = next((v for v in vulns if v.id == vuln_id), None)
    if not vuln:
        raise HTTPException(404, f"Vulnerability {vuln_id} not found in project {project_id}")
    return "\n".join(_format_vuln_md(vuln, project_title=proj["title"]))


@router.get("/projects/{project_id}/export")
def export_project(project_id: str, format: str = "yaml"):
    if format not in ("yaml", "timeline", "vulns_md"):
        raise HTTPException(400, "Supported formats: yaml, timeline, vulns_md")

    with get_conn() as conn:
        if format == "timeline":
            text = _export_timeline(conn, project_id)
        elif format == "vulns_md":
            text = _export_vulns_md(conn, project_id)
        else:
            text = _export_yaml(conn, project_id)

        return Response(content=text, media_type="text/plain")


@router.get("/projects/{project_id}/vulns/{vuln_id}/export")
def export_single_vuln(project_id: str, vuln_id: str):
    with get_conn() as conn:
        text = _export_single_vuln_md(conn, project_id, vuln_id)
        return Response(content=text, media_type="text/markdown")
