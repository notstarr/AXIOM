"""crawler/state.py — state table read/write, dedup, and YAML rendering."""

from __future__ import annotations

import json
import pathlib
import re
from typing import Any
from urllib.parse import urlsplit, parse_qsl, urlencode

import yaml

from axiom.server.db import get_conn
from axiom.server.services import utcnow

from .constants import CRAWLER_VIEW_FILES, CRAWLER_TRAFFIC_FILE
from .meta import read_meta


# ── Normalization ──────────────────────────

def normalize_target_domain(target_url: str) -> str:
    return urlsplit(target_url.strip()).hostname or ""


def slug_token(value: str, *, default: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return slug or default


def normalize_url(url: str) -> str:
    if not url:
        return ""
    try:
        parsed = urlsplit(url.strip())
    except Exception:
        return ""
    if not parsed.scheme or not parsed.netloc:
        return ""
    host = parsed.netloc.lower()
    path = parsed.path.rstrip("/") or "/"
    if parsed.query:
        params = sorted(parse_qsl(parsed.query, keep_blank_values=True))
        query = urlencode(params)
    else:
        query = ""
    return f"{parsed.scheme}://{host}{path}" + (f"?{query}" if query else "")


def normalize_api_path(path: str) -> str:
    if not path:
        return "/"
    segments = path.strip("/").split("/")
    normalized = []
    for seg in segments:
        if not seg:
            continue
        if re.match(r"^\d+$", seg):
            normalized.append("{id}")
        elif re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", seg, re.I):
            normalized.append("{id}")
        elif re.match(r"^[0-9a-f]{32,}$", seg, re.I):
            normalized.append("{id}")
        elif re.match(r"^\{[a-z_]+\}$", seg, re.I):
            normalized.append("{id}")
        else:
            normalized.append(seg)
    return "/" + "/".join(normalized) if normalized else "/"


def api_signature(method: str, path: str, full_url: str = "") -> str:
    norm_path = normalize_api_path(path)
    ajax = ""
    if full_url and "isAjax" in full_url:
        ajax = ":ajax"
    return f"{method.upper()} {norm_path}{ajax}"


# ── State table helpers ────────────────────

def state_id_column(table: str) -> str:
    return {
        "crawler_pages": "page_id",
        "crawler_apis": "api_id",
        "crawler_page_maps": "map_id",
    }[table]


def load_state_rows(group_id: str, table: str) -> list[dict[str, Any]]:
    id_column = state_id_column(table)
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT data_json FROM {table} WHERE group_id = ? ORDER BY updated_at, {id_column}",
            (group_id,),
        ).fetchall()
    loaded: list[dict[str, Any]] = []
    for row in rows:
        try:
            parsed = json.loads(row["data_json"] or "{}")
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            loaded.append(parsed)
    return loaded


def state_has_rows(group_id: str, table: str) -> bool:
    id_column = state_id_column(table)
    try:
        with get_conn() as conn:
            row = conn.execute(
                f"SELECT {id_column} FROM {table} WHERE group_id = ? LIMIT 1",
                (group_id,),
            ).fetchone()
    except AssertionError:
        return False
    return row is not None


def target_url_for_group(group_id: str) -> str:
    m = read_meta(group_id)
    target_url = str(m.get("target_url") or "").strip()
    return target_url


def render_state_snapshot(group_id: str, filename: str, *, target_url: str | None = None) -> str:
    if filename not in CRAWLER_VIEW_FILES:
        raise FileNotFoundError(filename)
    table, root_key = CRAWLER_VIEW_FILES[filename]
    target = target_url or target_url_for_group(group_id)
    payload = {
        "version": 1,
        "target": {
            "url": target,
            "domain": normalize_target_domain(target),
            "started_at": utcnow(),
        },
        root_key: load_state_rows(group_id, table),
    }
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)


def state_view_exists(group_id: str, filename: str) -> bool:
    if filename not in CRAWLER_VIEW_FILES:
        return False
    table, _root_key = CRAWLER_VIEW_FILES[filename]
    return state_has_rows(group_id, table)


# ── Upsert + merge ─────────────────────────

def _merge_unique_list(existing: list[Any], incoming: list[Any]) -> list[Any]:
    seen = {json.dumps(item, ensure_ascii=False, sort_keys=True) for item in existing}
    merged = list(existing)
    for item in incoming:
        key = json.dumps(item, ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged


def upsert_state_row(conn, table: str, group_id: str, row_id: str, payload: dict[str, Any]) -> None:
    id_column = state_id_column(table)
    existing = conn.execute(
        f"SELECT data_json FROM {table} WHERE group_id = ? AND {id_column} = ?",
        (group_id, row_id),
    ).fetchone()
    merged = payload
    if existing:
        try:
            current = json.loads(existing["data_json"] or "{}")
        except json.JSONDecodeError:
            current = {}
        if isinstance(current, dict):
            merged = dict(current)
            for key, value in payload.items():
                if isinstance(value, list) and isinstance(merged.get(key), list):
                    merged[key] = _merge_unique_list(merged[key], value)
                elif value not in (None, "", [], {}):
                    merged[key] = value
    conn.execute(
        f"""
        INSERT INTO {table} (group_id, {id_column}, data_json, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(group_id, {id_column}) DO UPDATE SET
            data_json = excluded.data_json,
            updated_at = excluded.updated_at
        """,
        (group_id, row_id, json.dumps(merged, ensure_ascii=False, separators=(",", ":")), utcnow()),
    )


# ── Dedup lookups ──────────────────────────

def find_existing_page_by_url(conn, group_id: str, url: str) -> str | None:
    norm = normalize_url(url)
    if not norm:
        return None
    rows = conn.execute(
        "SELECT page_id, data_json FROM crawler_pages WHERE group_id = ?",
        (group_id,),
    ).fetchall()
    for row in rows:
        try:
            data = json.loads(row["data_json"] or "{}")
        except json.JSONDecodeError:
            continue
        if normalize_url(str(data.get("url") or "")) == norm:
            return str(row["page_id"])
    return None


def find_existing_api_by_method_path(conn, group_id: str, method: str, path: str,
                                     full_url: str = "") -> str | None:
    target_sig = api_signature(method, path, full_url)
    rows = conn.execute(
        "SELECT api_id, data_json FROM crawler_apis WHERE group_id = ?",
        (group_id,),
    ).fetchall()
    for row in rows:
        try:
            data = json.loads(row["data_json"] or "{}")
        except json.JSONDecodeError:
            continue
        existing_method = str(data.get("method") or "").upper()
        existing_path = str(data.get("path") or "")
        existing_url = str(data.get("full_url_example") or "")
        existing_sig = api_signature(existing_method, existing_path, existing_url)
        if existing_sig == target_sig:
            return str(row["api_id"])
    return None


def find_existing_mapping_by_page(conn, group_id: str, page_url: str) -> str | None:
    norm = normalize_url(page_url)
    if not norm:
        return None
    rows = conn.execute(
        "SELECT map_id, data_json FROM crawler_page_maps WHERE group_id = ?",
        (group_id,),
    ).fetchall()
    for row in rows:
        try:
            data = json.loads(row["data_json"] or "{}")
        except json.JSONDecodeError:
            continue
        if normalize_url(str(data.get("page_url") or "")) == norm:
            return str(row["map_id"])
    return None


# ── Apply increment ────────────────────────

def apply_crawler_increment(group_id: str, increment: dict[str, Any]) -> tuple[int, int, int]:
    new_pages = 0
    new_apis = 0
    new_maps = 0
    with get_conn() as conn:
        for page in increment.get("pages", []):
            page_url = str(page.get("url") or "")
            page_id = str(page["id"])
            existing_id = find_existing_page_by_url(conn, group_id, page_url) if page_url else None
            if existing_id:
                upsert_state_row(conn, "crawler_pages", group_id, existing_id, page)
            else:
                exists = conn.execute(
                    "SELECT 1 FROM crawler_pages WHERE group_id = ? AND page_id = ?",
                    (group_id, page_id),
                ).fetchone()
                if not exists:
                    new_pages += 1
                upsert_state_row(conn, "crawler_pages", group_id, page_id, page)

        for api in increment.get("apis", []):
            method = str(api.get("method") or "GET").upper()
            path = str(api.get("path") or "/")
            full_url = str(api.get("full_url_example") or api.get("url") or "")
            api_id = str(api["id"])
            existing_id = find_existing_api_by_method_path(conn, group_id, method, path, full_url)
            if existing_id:
                upsert_state_row(conn, "crawler_apis", group_id, existing_id, api)
            else:
                exists = conn.execute(
                    "SELECT 1 FROM crawler_apis WHERE group_id = ? AND api_id = ?",
                    (group_id, api_id),
                ).fetchone()
                if not exists:
                    new_apis += 1
                upsert_state_row(conn, "crawler_apis", group_id, api_id, api)

        for mapping in increment.get("mappings", []):
            page_url = str(mapping.get("page_url") or "")
            map_id = str(mapping["id"])
            existing_id = find_existing_mapping_by_page(conn, group_id, page_url) if page_url else None
            if existing_id:
                upsert_state_row(conn, "crawler_page_maps", group_id, existing_id, mapping)
            else:
                exists = conn.execute(
                    "SELECT 1 FROM crawler_page_maps WHERE group_id = ? AND map_id = ?",
                    (group_id, map_id),
                ).fetchone()
                if not exists:
                    new_maps += 1
                upsert_state_row(conn, "crawler_page_maps", group_id, map_id, mapping)
    return (new_pages, new_apis, new_maps)


# ── Schema file I/O (legacy compat) ────────

def load_yaml_file(path: pathlib.Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def persist_schema_files_into_state(group_id: str, ai_dir: str) -> None:
    base = pathlib.Path(ai_dir)
    site_map = load_yaml_file(base / "site_map.yaml")
    api_inventory = load_yaml_file(base / "api_inventory.yaml")
    page_api_map = load_yaml_file(base / "page_api_map.yaml")
    with get_conn() as conn:
        for page in site_map.get("pages", []) or []:
            if isinstance(page, dict) and page.get("id"):
                upsert_state_row(conn, "crawler_pages", group_id, str(page["id"]), page)
        for api in api_inventory.get("apis", []) or []:
            if isinstance(api, dict) and api.get("id"):
                upsert_state_row(conn, "crawler_apis", group_id, str(api["id"]), api)
        for mapping in page_api_map.get("mappings", []) or []:
            if isinstance(mapping, dict) and mapping.get("id"):
                upsert_state_row(conn, "crawler_page_maps", group_id, str(mapping["id"]), mapping)


def export_state_to_schema_files(group_id: str, ai_dir: str, target_url: str) -> None:
    base = pathlib.Path(ai_dir)
    base.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        pages = [json.loads(row["data_json"]) for row in conn.execute(
            "SELECT data_json FROM crawler_pages WHERE group_id = ? ORDER BY updated_at, page_id", (group_id,)
        ).fetchall()]
        apis = [json.loads(row["data_json"]) for row in conn.execute(
            "SELECT data_json FROM crawler_apis WHERE group_id = ? ORDER BY updated_at, api_id", (group_id,)
        ).fetchall()]
        mappings = [json.loads(row["data_json"]) for row in conn.execute(
            "SELECT data_json FROM crawler_page_maps WHERE group_id = ? ORDER BY updated_at, map_id", (group_id,)
        ).fetchall()]
    target = {
        "url": target_url,
        "domain": normalize_target_domain(target_url),
        "started_at": utcnow(),
    }
    (base / "site_map.yaml").write_text(yaml.safe_dump({"version": 1, "target": target, "pages": pages}, allow_unicode=True, sort_keys=False), encoding="utf-8")
    (base / "api_inventory.yaml").write_text(yaml.safe_dump({"version": 1, "target": target, "apis": apis}, allow_unicode=True, sort_keys=False), encoding="utf-8")
    (base / "page_api_map.yaml").write_text(yaml.safe_dump({"version": 1, "target": target, "mappings": mappings}, allow_unicode=True, sort_keys=False), encoding="utf-8")
