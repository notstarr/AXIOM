from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query

from axiom.server.db import get_conn
from axiom.server.models import (
    AssetRead, AssetCreate, AssetUpdate, AssetStats,
    CredentialRead, CredentialCreate, CredentialUpdate,
)

router = APIRouter(tags=["assets"])


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_ASSET_STATUS_RANK = {
    "untested": 0,
    "blocked": 1,
    "safe": 2,
    "vulnerable": 3,
}


def _has_text(value: str | None) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _status_rank(value: str | None) -> int:
    if not value:
        return -1
    return _ASSET_STATUS_RANK.get(value, -1)




def _merge_asset_updates(existing: sqlite3.Row, body: AssetCreate, now: str) -> dict[str, object]:
    updates: dict[str, object] = {}
    candidate_fields = (
        "group_id",
        "frontend_domain",
        "frontend_url",
        "backend_domain",
        "api_purpose",
        "auth_required",
        "auth_type",
        "source_file",
        "sample_body",
        "content_type",
        "login_config_json",
    )
    for field in candidate_fields:
        incoming = getattr(body, field, None)
        current = existing[field]
        if field == "auth_required":
            # Keep explicit values over unknown placeholders.
            if incoming == "unknown" and _has_text(current):
                continue
        if _has_text(incoming) and incoming != current:
            updates[field] = incoming

    incoming_status = body.test_status
    current_status = existing["test_status"]
    if _status_rank(incoming_status) > _status_rank(current_status):
        updates["test_status"] = incoming_status
        updates["tested_at"] = now
    return updates




# ── Assets ────────────────────────────────────────────────────────────

@router.get("/assets", response_model=list[AssetRead])
def list_assets(
    project_id: str = Query(default=""),
    group_id: str = Query(default=""),
    backend_domain: str = Query(default=""),
    test_status: str = Query(default=""),
    api_method: str = Query(default=""),
    search: str = Query(default=""),
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    with get_conn() as conn:
        where = []
        params: list = []
        if project_id:
            where.append("project_id = ?")
            params.append(project_id)
        if group_id:
            where.append("group_id = ?")
            params.append(group_id)
        if backend_domain:
            where.append("backend_domain LIKE ?")
            params.append(f"%{backend_domain}%")
        if test_status:
            where.append("test_status = ?")
            params.append(test_status)
        if api_method:
            where.append("api_method = ?")
            params.append(api_method.upper())
        if search:
            where.append("(api_path LIKE ? OR api_purpose LIKE ? OR backend_domain LIKE ?)")
            q = f"%{search}%"
            params.extend([q, q, q])
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        rows = conn.execute(
            f"SELECT * FROM assets{clause} ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
        return [AssetRead(**dict(r)) for r in rows]


@router.get("/assets/domains")
def list_asset_domains():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT backend_domain FROM assets WHERE backend_domain IS NOT NULL AND backend_domain != '' ORDER BY backend_domain"
        ).fetchall()
        return [r["backend_domain"] for r in rows]


@router.get("/assets/projects")
def list_asset_projects():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT project_id FROM assets ORDER BY project_id"
        ).fetchall()
        return [r["project_id"] for r in rows]


@router.get("/assets/groups")
def list_asset_groups():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT group_id FROM assets WHERE group_id != '' ORDER BY group_id"
        ).fetchall()
        return [r["group_id"] for r in rows]


@router.get("/assets/login-configs")
def list_login_configs(
    group_id: str = Query(default=""),
):
    with get_conn() as conn:
        where = ["login_config_json IS NOT NULL", "login_config_json != ''"]
        params: list = []
        if group_id:
            where.append("group_id = ?")
            params.append(group_id)
        clause = " WHERE " + " AND ".join(where)
        rows = conn.execute(
            f"SELECT * FROM assets{clause} ORDER BY id DESC LIMIT 50",
            params,
        ).fetchall()
        return [AssetRead(**dict(r)) for r in rows]


@router.get("/assets/stats", response_model=AssetStats)
def get_asset_stats(
    project_id: str = Query(default=""),
    group_id: str = Query(default=""),
):
    with get_conn() as conn:
        where = []
        params: list = []
        if project_id:
            where.append("project_id = ?")
            params.append(project_id)
        if group_id:
            where.append("group_id = ?")
            params.append(group_id)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        rows = conn.execute(
            f"SELECT test_status, COUNT(*) AS cnt FROM assets{clause} GROUP BY test_status",
            params,
        ).fetchall()
        counts = {r["test_status"]: r["cnt"] for r in rows}
        total = sum(counts.values())
        return AssetStats(
            total=total,
            safe=counts.get("safe", 0),
            blocked=counts.get("blocked", 0),
            vulnerable=counts.get("vulnerable", 0),
            untested=counts.get("untested", 0),
        )


@router.get("/assets/{asset_id}", response_model=AssetRead)
def get_asset(asset_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
        if not row:
            raise HTTPException(404, "asset not found")
        return AssetRead(**dict(row))


@router.post("/assets", response_model=AssetRead)
def create_asset(body: AssetCreate):
    with get_conn() as conn:
        now = _now_iso()
        try:
            cur = conn.execute(
                """INSERT INTO assets (project_id, group_id, frontend_domain, frontend_url, backend_domain,
                   api_path, api_method, api_purpose, auth_required, auth_type, test_status, source_file,
                   sample_body, content_type, login_config_json, discovered_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (body.project_id, body.group_id, body.frontend_domain, body.frontend_url, body.backend_domain,
                 body.api_path, body.api_method.upper(), body.api_purpose,
                 body.auth_required, body.auth_type, body.test_status, body.source_file,
                 body.sample_body, body.content_type, body.login_config_json, now),
            )
        except sqlite3.IntegrityError:
            row = conn.execute(
                """
                SELECT * FROM assets
                WHERE project_id = ? AND backend_domain = ? AND api_path = ? AND api_method = ?
                """,
                (body.project_id, body.backend_domain, body.api_path, body.api_method.upper()),
            ).fetchone()
            if not row:
                raise HTTPException(409, "asset already exists (duplicate project+domain+path+method)")
            updates = _merge_asset_updates(row, body, now)
            if updates:
                sets = ", ".join(f"{key} = ?" for key in updates)
                conn.execute(
                    f"UPDATE assets SET {sets} WHERE id = ?",
                    [*updates.values(), row["id"]],
                )
            row = conn.execute("SELECT * FROM assets WHERE id = ?", (row["id"],)).fetchone()
            return AssetRead(**dict(row))
        row = conn.execute("SELECT * FROM assets WHERE id = ?", (cur.lastrowid,)).fetchone()
        return AssetRead(**dict(row))


@router.put("/assets/{asset_id}", response_model=AssetRead)
def update_asset(asset_id: int, body: AssetUpdate):
    with get_conn() as conn:
        row = conn.execute("SELECT id, test_status FROM assets WHERE id = ?", (asset_id,)).fetchone()
        if not row:
            raise HTTPException(404, "asset not found")
        updates = {}
        if body.api_purpose is not None:
            updates["api_purpose"] = body.api_purpose
        if body.test_status is not None:
            updates["test_status"] = body.test_status
            # Auto-set tested_at when status changes
            if body.test_status != row["test_status"]:
                updates["tested_at"] = _now_iso()
        if body.auth_required is not None:
            updates["auth_required"] = body.auth_required
        if body.auth_type is not None:
            updates["auth_type"] = body.auth_type
        if body.known_vulns is not None:
            updates["known_vulns"] = body.known_vulns
        if body.login_config_json is not None:
            updates["login_config_json"] = body.login_config_json
        if updates:
            sets = ", ".join(f"{k} = ?" for k in updates)
            conn.execute(f"UPDATE assets SET {sets} WHERE id = ?", list(updates.values()) + [asset_id])
        row = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
        return AssetRead(**dict(row))


@router.delete("/assets/{asset_id}")
def delete_asset(asset_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM assets WHERE id = ?", (asset_id,)).fetchone()
        if not row:
            raise HTTPException(404, "asset not found")
        conn.execute("DELETE FROM assets WHERE id = ?", (asset_id,))
        return {"ok": True}


# ── Credentials ───────────────────────────────────────────────────────

@router.get("/credentials", response_model=list[CredentialRead])
def list_credentials(
    group_id: str = Query(default=""),
    active_only: bool = Query(default=True),
):
    with get_conn() as conn:
        where = []
        params: list = []
        if group_id:
            where.append("group_id = ?")
            params.append(group_id)
        if active_only:
            where.append("active = 1")
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        rows = conn.execute(
            f"SELECT * FROM credentials{clause} ORDER BY captured_at DESC",
            params,
        ).fetchall()
        return [CredentialRead(**dict(r)) for r in rows]


@router.get("/credentials/{cred_id}", response_model=CredentialRead)
def get_credential(cred_id: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM credentials WHERE id = ?", (cred_id,)).fetchone()
        if not row:
            raise HTTPException(404, "credential not found")
        return CredentialRead(**dict(row))


@router.post("/credentials", response_model=CredentialRead)
def create_credential(body: CredentialCreate):
    # label 现在直接存储人工/智能体写入的原始值（显示为“名称”），不再强制规整成 pg.xxx.type.xxx
    raw_label = (body.label or "").strip()
    now = _now_iso()
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT * FROM credentials WHERE group_id = ? AND label = ?",
            (body.group_id, raw_label),
        ).fetchone()
        if existing:
            updates: dict[str, object] = {
                "auth_type": body.auth_type,
                "credentials_json": body.credentials_json,
                "active": body.active,
                "captured_at": now,
            }
            if _has_text(body.domains_json):
                updates["domains_json"] = body.domains_json
            if _has_text(body.source_task_id):
                updates["source_task_id"] = body.source_task_id
            if body.expires_at is not None:
                updates["expires_at"] = body.expires_at
            if body.asset_id is not None:
                updates["asset_id"] = body.asset_id
            sets = ", ".join(f"{k} = ?" for k in updates)
            conn.execute(
                f"UPDATE credentials SET {sets} WHERE id = ?",
                [*updates.values(), existing["id"]],
            )
            row = conn.execute("SELECT * FROM credentials WHERE id = ?", (existing["id"],)).fetchone()
            return CredentialRead(**dict(row))

        cred_id = f"cred_{uuid.uuid4().hex[:12]}"
        conn.execute(
            """INSERT INTO credentials (id, group_id, label, auth_type, credentials_json, domains_json,
               source_task_id, captured_at, expires_at, active, asset_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (cred_id, body.group_id, raw_label, body.auth_type,
             body.credentials_json, body.domains_json, body.source_task_id, now, body.expires_at, body.active, body.asset_id),
        )
        row = conn.execute("SELECT * FROM credentials WHERE id = ?", (cred_id,)).fetchone()
        return CredentialRead(**dict(row))


@router.put("/credentials/{cred_id}", response_model=CredentialRead)
def update_credential(cred_id: str, body: CredentialUpdate):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM credentials WHERE id = ?", (cred_id,)).fetchone()
        if not row:
            raise HTTPException(404, "credential not found")
        updates = {}
        if body.label is not None:
            updates["label"] = body.label.strip()
        if body.auth_type is not None:
            updates["auth_type"] = body.auth_type
        if body.credentials_json is not None:
            updates["credentials_json"] = body.credentials_json
        if body.domains_json is not None:
            updates["domains_json"] = body.domains_json
        if body.expires_at is not None:
            updates["expires_at"] = body.expires_at
        if body.active is not None:
            updates["active"] = 1 if body.active else 0
        if body.asset_id is not None:
            updates["asset_id"] = body.asset_id
        if updates:
            sets = ", ".join(f"{k} = ?" for k in updates)
            try:
                conn.execute(f"UPDATE credentials SET {sets} WHERE id = ?", list(updates.values()) + [cred_id])
            except sqlite3.IntegrityError:
                raise HTTPException(409, "credential with same group_id+label already exists")
        row = conn.execute("SELECT * FROM credentials WHERE id = ?", (cred_id,)).fetchone()
        return CredentialRead(**dict(row))


@router.delete("/credentials/{cred_id}")
def delete_credential(cred_id: str):
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM credentials WHERE id = ?", (cred_id,)).fetchone()
        if not row:
            raise HTTPException(404, "credential not found")
        conn.execute("DELETE FROM credentials WHERE id = ?", (cred_id,))
        return {"ok": True}
