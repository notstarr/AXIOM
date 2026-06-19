from __future__ import annotations

import json
import logging
import sqlite3

from axiom.server.db import current_db_path

LOG = logging.getLogger(__name__)


def load_domain_blacklist(project_id: str | None = None) -> set[str]:
    db_path = current_db_path()
    if not db_path.exists():
        return set()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT domain_blacklist_json
            FROM settings
            WHERE rowid = 1
            """
        ).fetchone()
        project_row = None
        if project_id:
            project_row = conn.execute(
                """
                SELECT g.scope_json
                FROM projects p
                JOIN project_groups g ON g.id = p.parent_project_id
                WHERE p.id = ?
                """,
                (project_id,),
            ).fetchone()
    except sqlite3.Error as exc:
        LOG.warning("failed to read domain blacklist error=%s", exc)
        return set()
    finally:
        conn.close()
    if row is None:
        values: set[str] = set()
    else:
        values = _parse_domain_list(row["domain_blacklist_json"] or "[]")
    if project_id and "project_row" in locals() and project_row is not None:
        try:
            scope = json.loads(project_row["scope_json"] or "{}")
        except json.JSONDecodeError:
            scope = {}
        if isinstance(scope, dict):
            values.update(_normalize_domains(scope.get("exclude", [])))
    return values


def _parse_domain_list(raw: str) -> set[str]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return set()
    if not isinstance(payload, list):
        return set()
    return _normalize_domains(payload)


def _normalize_domains(payload: object) -> set[str]:
    values: set[str] = set()
    if not isinstance(payload, list):
        return values
    for item in payload:
        text = str(item).strip().lower().strip(".")
        if text:
            values.add(text)
    return values
