from fastapi import APIRouter, HTTPException, Query
import sqlite3
from pathlib import Path

from axiom.server.models import AssetRead, AssetCreate, AssetUpdate, AssetStats

router = APIRouter(tags=["assets"])
ASSETS_DB = Path("/tmp/axiom-assets.db")


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(ASSETS_DB))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@router.get("/assets", response_model=list[AssetRead])
def list_assets(
    project_id: str = Query(default=""),
    backend_domain: str = Query(default=""),
    test_status: str = Query(default=""),
    api_method: str = Query(default=""),
    search: str = Query(default=""),
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    with _get_conn() as conn:
        where = []
        params: list = []
        if project_id:
            where.append("project_id = ?")
            params.append(project_id)
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
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT backend_domain FROM assets WHERE backend_domain IS NOT NULL AND backend_domain != '' ORDER BY backend_domain"
        ).fetchall()
        return [r["backend_domain"] for r in rows]


@router.get("/assets/projects")
def list_asset_projects():
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT project_id FROM assets ORDER BY project_id"
        ).fetchall()
        return [r["project_id"] for r in rows]


@router.get("/assets/stats", response_model=AssetStats)
def get_asset_stats(
    project_id: str = Query(default=""),
):
    with _get_conn() as conn:
        where_clause = ""
        params: list = []
        if project_id:
            where_clause = " WHERE project_id = ?"
            params = [project_id]
        total = conn.execute(f"SELECT COUNT(*) FROM assets{where_clause}", params).fetchone()[0]
        safe = conn.execute(f"SELECT COUNT(*) FROM assets{where_clause}{' AND' if where_clause else ' WHERE'} test_status='safe'", params).fetchone()[0]
        blocked = conn.execute(f"SELECT COUNT(*) FROM assets{where_clause}{' AND' if where_clause else ' WHERE'} test_status='blocked'", params).fetchone()[0]
        vulnerable = conn.execute(f"SELECT COUNT(*) FROM assets{where_clause}{' AND' if where_clause else ' WHERE'} test_status='vulnerable'", params).fetchone()[0]
        untested = conn.execute(f"SELECT COUNT(*) FROM assets{where_clause}{' AND' if where_clause else ' WHERE'} test_status='untested'", params).fetchone()[0]
        return AssetStats(total=total, safe=safe, blocked=blocked, vulnerable=vulnerable, untested=untested)


@router.get("/assets/{asset_id}", response_model=AssetRead)
def get_asset(asset_id: int):
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
        if not row:
            raise HTTPException(404, "asset not found")
        return AssetRead(**dict(row))


@router.post("/assets", response_model=AssetRead)
def create_asset(body: AssetCreate):
    with _get_conn() as conn:
        try:
            cur = conn.execute(
                """INSERT INTO assets (project_id, frontend_domain, frontend_url, backend_domain,
                   api_path, api_method, api_purpose, auth_required, auth_type, test_status, source_file)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (body.project_id, body.frontend_domain, body.frontend_url, body.backend_domain,
                 body.api_path, body.api_method.upper(), body.api_purpose,
                 body.auth_required, body.auth_type, body.test_status, body.source_file),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(409, "asset already exists (duplicate project+domain+path+method)")
        return get_asset(cur.lastrowid)


@router.put("/assets/{asset_id}", response_model=AssetRead)
def update_asset(asset_id: int, body: AssetUpdate):
    with _get_conn() as conn:
        row = conn.execute("SELECT id FROM assets WHERE id = ?", (asset_id,)).fetchone()
        if not row:
            raise HTTPException(404, "asset not found")
        updates = {}
        if body.api_purpose is not None:
            updates["api_purpose"] = body.api_purpose
        if body.test_status is not None:
            updates["test_status"] = body.test_status
        if body.auth_required is not None:
            updates["auth_required"] = body.auth_required
        if body.auth_type is not None:
            updates["auth_type"] = body.auth_type
        if body.known_vulns is not None:
            updates["known_vulns"] = body.known_vulns
        if updates:
            sets = ", ".join(f"{k} = ?" for k in updates)
            conn.execute(f"UPDATE assets SET {sets} WHERE id = ?", list(updates.values()) + [asset_id])
        return get_asset(asset_id)


@router.delete("/assets/{asset_id}")
def delete_asset(asset_id: int):
    with _get_conn() as conn:
        row = conn.execute("SELECT id FROM assets WHERE id = ?", (asset_id,)).fetchone()
        if not row:
            raise HTTPException(404, "asset not found")
        conn.execute("DELETE FROM assets WHERE id = ?", (asset_id,))
        return {"ok": True}
