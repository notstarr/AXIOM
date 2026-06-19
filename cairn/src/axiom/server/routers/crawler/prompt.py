"""crawler/prompt.py — prompt building and state context injection."""

from __future__ import annotations

import pathlib

from axiom.server.services import utcnow

from .state import (
    load_state_rows,
    normalize_url,
    normalize_target_domain,
)


def build_crawler_state_context(group_id: str) -> str:
    pages = load_state_rows(group_id, "crawler_pages")
    if not pages:
        return "（暂无已发现页面，这是第一轮探索）"

    discovered_lines = []
    discovered_urls = set()
    for p in pages:
        url = str(p.get("url") or "")
        if not url:
            continue
        norm = normalize_url(url)
        if norm in discovered_urls:
            continue
        discovered_urls.add(norm)
        ptype = p.get("page_type") or "unknown"
        exhausted = "已穷尽" if p.get("exhausted") else ""
        tag = f" [{ptype}]{(' ' + exhausted) if exhausted else ''}"
        discovered_lines.append(f"- {url}{tag}")

    frontier_urls = []
    seen_frontier = set()
    for p in pages:
        for action in (p.get("actions") or []):
            href = str(action.get("href") or action.get("target_hint") or "")
            if not href or not href.startswith("http"):
                continue
            norm = normalize_url(href)
            if norm in discovered_urls or norm in seen_frontier:
                continue
            seen_frontier.add(norm)
            label = action.get("name") or action.get("label") or ""
            frontier_urls.append(f"- {href}" + (f" ({label})" if label else ""))

    parts = [f"已发现 {len(discovered_lines)} 个页面:"]
    parts.extend(discovered_lines[:40])
    if len(discovered_lines) > 40:
        parts.append(f"... (另有 {len(discovered_lines) - 40} 个页面省略)")

    if frontier_urls:
        parts.append("")
        parts.append(f"未访问的链接 ({len(frontier_urls)} 个, 建议优先探索):")
        parts.extend(frontier_urls[:15])
        if len(frontier_urls) > 15:
            parts.append(f"... (另有 {len(frontier_urls) - 15} 个未访问链接)")

    return "\n".join(parts)


def build_prompt(template_path: pathlib.Path, *, session_id: str, target_url: str, ai_dir: str,
                 group_id: str = "", round_num: int = 0, total_rounds: int = 0) -> str:
    prompt_template = template_path.read_text(encoding="utf-8")
    intent_description = f"爬取目标站点 {target_url}，产出站点地图、API 清单和页面-API 映射到 {ai_dir}"
    state_context = build_crawler_state_context(group_id) if group_id else ""
    round_info = f"当前第 {round_num} 轮" if round_num else ""
    if total_rounds:
        round_info += f", 预计剩余约 {max(0, total_rounds - round_num)} 轮"
    return (
        prompt_template
        .replace("{intent_id}", session_id)
        .replace("{intent_description}", intent_description)
        .replace("{ai_dir}", ai_dir)
        .replace("{crawler_state}", state_context)
        .replace("{round_info}", round_info)
        .replace("<TARGET_DOMAIN>", normalize_target_domain(target_url))
        .replace("<TARGET_URL>", target_url)
        .replace("<NOW_UTC>", utcnow())
    )
