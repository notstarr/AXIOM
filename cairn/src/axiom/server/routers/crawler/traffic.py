"""crawler/traffic.py — traffic file reading, record creation, and merge."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit

from axiom.server.db import get_conn

from .constants import CRAWLER_TRAFFIC_FILE as _TRAFFIC_FILE_CONST
from .state import (
    slug_token,
    normalize_url,
    normalize_api_path,
    api_signature,
    upsert_state_row,
    find_existing_page_by_url,
    find_existing_api_by_method_path,
    find_existing_mapping_by_page,
)
from .meta import read_meta


def _iso_to_epoch(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        from datetime import datetime, timezone
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return 0.0


def traffic_since_epoch(meta: dict[str, Any]) -> float:
    return _iso_to_epoch(str(meta.get("last_flush_at") or "")) or _iso_to_epoch(str(meta.get("started_at") or ""))


def load_recent_traffic_flows(meta: dict[str, Any]) -> list[dict[str, Any]]:
    from . import constants as _const
    path = _const.CRAWLER_TRAFFIC_FILE
    if not path.exists():
        return []
    lower_bound = traffic_since_epoch(meta)
    flows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    flow = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(flow, dict):
                    continue
                try:
                    ts = float(flow.get("ts") or 0)
                except (TypeError, ValueError):
                    ts = 0.0
                if ts and ts < lower_bound:
                    continue
                flows.append(flow)
    except OSError:
        return []
    return flows


def flow_referer_url(flow: dict[str, Any]) -> str:
    headers = flow.get("request_headers") or {}
    if not isinstance(headers, dict):
        return ""
    referer = headers.get("referer") or headers.get("Referer") or ""
    return str(referer).strip()


def flow_is_relevant_api(flow: dict[str, Any]) -> bool:
    method = str(flow.get("method") or "").upper()
    if method in {"", "CONNECT", "OPTIONS"}:
        return False
    url = str(flow.get("url") or "")
    if "/api/" in url:
        return True
    content_type = str(flow.get("content_type") or "").lower()
    return any(token in content_type for token in ("application/json", "application/xml", "text/xml", "application/graphql", "text/plain"))


def make_page_record(page_url: str) -> dict[str, Any] | None:
    parsed = urlsplit(page_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    path = parsed.path or "/"
    page_slug = slug_token(f"{parsed.netloc}_{path}", default="page")
    page_type = "form" if path.endswith("/new") else "unknown"
    name_bits = [bit for bit in path.strip("/").split("/") if bit]
    page_name = " / ".join(name_bits) if name_bits else parsed.netloc
    return {
        "id": f"page_{page_slug}",
        "name": page_name,
        "url": page_url,
        "path": path,
        "page_type": page_type,
        "parent_id": None,
        "discovered_via": {"type": "unknown", "source_page_id": None, "trigger_label": "traffic fallback"},
        "requires_auth": False,
        "business_role": "unknown",
        "preconditions": [],
        "actions": [],
        "key_data": [],
        "related_api_ids": [],
        "notes": ["traffic fallback"],
        "exhausted": False,
    }


def make_api_record(flow: dict[str, Any], page: dict[str, Any] | None) -> dict[str, Any] | None:
    url = str(flow.get("url") or "")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    method = str(flow.get("method") or "GET").upper()
    path = parsed.path or "/"
    status_code = int(flow.get("status_code") or 0)
    api_slug = slug_token(f"{parsed.netloc}_{method}_{path}", default="api")
    response_keys: list[str] = []
    response_body = str(flow.get("response_body") or "")
    try:
        body_json = json.loads(response_body) if response_body else None
        if isinstance(body_json, dict):
            response_keys = [str(key) for key in body_json.keys()]
    except json.JSONDecodeError:
        pass
    headers = flow.get("request_headers") or {}
    cookie_value = ""
    if isinstance(headers, dict):
        cookie_value = str(headers.get("cookie") or headers.get("Cookie") or "")
    notes = ["traffic fallback"]
    if status_code in {401, 403}:
        notes.append("captured as auth failure during flush fallback")
    return {
        "id": f"api_{api_slug}",
        "method": method,
        "path": path,
        "full_url_example": url,
        "purpose": f"{method} {parsed.netloc}{path}",
        "protocol": parsed.scheme,
        "content_type": str(flow.get("content_type") or ""),
        "status_codes_seen": [status_code] if status_code else [],
        "auth": {
            "required": status_code in {401, 403},
            "type": "cookie" if cookie_value else "unknown",
            "example": cookie_value[:120] or None,
        },
        "seen_on_pages": ([{"page_id": page["id"], "page_url": page["url"]}] if page else []),
        "params": [],
        "response_keys": response_keys,
        "produces_data": [],
        "depends_on": [],
        "missing_inputs": [],
        "notes": notes,
    }


def make_map_record(flow: dict[str, Any], page: dict[str, Any], api: dict[str, Any]) -> dict[str, Any]:
    path = str(api.get("path") or "/")
    method = str(api.get("method") or "GET")
    action_slug = slug_token(f"{method}_{path}", default="action")
    status_code = int((flow.get("status_code") or 0) if str(flow.get("status_code") or "").isdigit() else flow.get("status_code") or 0)
    result_status = "denied" if status_code in {401, 403} else ("failure" if status_code >= 400 else "success")
    return {
        "id": f"map_{page['id'][5:]}" if str(page.get("id", "")).startswith("page_") else f"map_{slug_token(page['id'], default='page')}",
        "page_id": page["id"],
        "page_name": page["name"],
        "page_url": page["url"],
        "on_load": [],
        "actions": [
            {
                "action_id": f"action_{action_slug}",
                "action_name": f"{method} {path}",
                "trigger_type": "form_submit" if method == "POST" else "click",
                "trigger_label": path,
                "target_hint": "traffic fallback",
                "result": {"navigation": False, "status": result_status},
                "apis": [{"api_id": api["id"], "method": method, "path": path}],
                "notes": ["traffic fallback"],
            }
        ],
    }


def merge_recent_traffic_into_state(meta: dict[str, Any]) -> tuple[int, int, int]:
    flows = [flow for flow in load_recent_traffic_flows(meta) if flow_is_relevant_api(flow)]
    if not flows:
        return (0, 0, 0)
    project_id = str(meta["project_id"])
    with get_conn() as conn:
        new_pages = 0
        new_apis = 0
        new_maps = 0
        for flow in flows:
            referer_url = flow_referer_url(flow)
            if referer_url:
                existing_page_id = find_existing_page_by_url(conn, project_id, referer_url)
                if existing_page_id:
                    page = None
                else:
                    page = make_page_record(referer_url)
            else:
                page = None

            api = make_api_record(flow, page)
            if api is None:
                continue

            existing_api_id = find_existing_api_by_method_path(
                conn, project_id,
                str(api.get("method") or "GET"),
                str(api.get("path") or "/"),
                str(api.get("full_url_example") or ""),
            )
            if existing_api_id:
                existing_row = conn.execute(
                    "SELECT data_json FROM crawler_apis WHERE group_id = ? AND api_id = ?",
                    (project_id, existing_api_id),
                ).fetchone()
                if existing_row:
                    try:
                        existing_data = json.loads(existing_row["data_json"] or "{}")
                    except json.JSONDecodeError:
                        existing_data = {}
                    seen = set(existing_data.get("status_codes_seen") or [])
                    for sc in (api.get("status_codes_seen") or []):
                        seen.add(sc)
                    existing_data["status_codes_seen"] = sorted(seen)
                    upsert_state_row(conn, "crawler_apis", project_id, existing_api_id, existing_data)
            else:
                api_exists = conn.execute(
                    "SELECT 1 FROM crawler_apis WHERE group_id = ? AND api_id = ?",
                    (project_id, api["id"]),
                ).fetchone()
                if not api_exists:
                    new_apis += 1
                upsert_state_row(conn, "crawler_apis", project_id, api["id"], api)

            if page is not None:
                existing_map_id = find_existing_mapping_by_page(conn, project_id, page["url"])
                if existing_map_id:
                    continue
                mapping = make_map_record(flow, page, api)
                map_exists = conn.execute(
                    "SELECT 1 FROM crawler_page_maps WHERE group_id = ? AND map_id = ?",
                    (project_id, mapping["id"]),
                ).fetchone()
                if not map_exists:
                    new_maps += 1
                upsert_state_row(conn, "crawler_page_maps", project_id, mapping["id"], mapping)

            if page is not None and not existing_page_id:
                page_exists = conn.execute(
                    "SELECT 1 FROM crawler_pages WHERE group_id = ? AND page_id = ?",
                    (project_id, page["id"]),
                ).fetchone()
                if not page_exists:
                    new_pages += 1
                upsert_state_row(conn, "crawler_pages", project_id, page["id"], page)
    return (new_pages, new_apis, new_maps)


def fallback_persist_recent_traffic(meta: dict[str, Any], log_f) -> bool:
    new_pages, new_apis, new_maps = merge_recent_traffic_into_state(meta)
    if new_pages == 0 and new_apis == 0 and new_maps == 0:
        return False
    description = f"backend fallback persisted {new_pages} new pages, {new_apis} new apis, {new_maps} new mappings from recent traffic."
    log_f.write(json.dumps({"accepted": True, "data": {"description": description}}, ensure_ascii=False) + "\n")
    log_f.flush()
    return True
