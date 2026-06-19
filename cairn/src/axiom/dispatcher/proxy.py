from __future__ import annotations

import logging
import sqlite3
from urllib.parse import urlparse

from axiom.server.db import current_db_path

LOG = logging.getLogger(__name__)


def proxy_env_for_phase(phase: str, *, api_base_url: str = "") -> dict[str, str]:
    """Return proxy env vars when the global proxy switch is enabled.

    Proxy applies to all task phases (bootstrap, reason, explore,
    healthcheck, and conclude variants).  The LLM API endpoint is
    excluded via NO_PROXY so that API calls go direct while tools
    (bash, curl, etc.) route through the proxy.
    """
    proxy_url = _load_proxy_url()
    if not proxy_url:
        return {}
    if not _load_proxy_enabled():
        return {}
    env: dict[str, str] = {
        "ALL_PROXY": proxy_url,
        "all_proxy": proxy_url,
        "HTTP_PROXY": proxy_url,
        "HTTPS_PROXY": proxy_url,
    }
    no_proxy = _build_no_proxy(api_base_url)
    if no_proxy:
        env["NO_PROXY"] = no_proxy
        env["no_proxy"] = no_proxy
    return env


def _build_no_proxy(api_base_url: str) -> str:
    """Extract the API hostname so it can bypass the proxy."""
    if not api_base_url:
        return ""
    try:
        host = urlparse(api_base_url).hostname
    except Exception:
        return ""
    if not host:
        return ""
    return host


def _load_proxy_url() -> str:
    db_path = current_db_path()
    if not db_path.exists():
        return ""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT proxy_url FROM settings WHERE rowid = 1"
        ).fetchone()
    except sqlite3.Error as exc:
        LOG.warning("failed to read proxy url error=%s", exc)
        return ""
    finally:
        conn.close()
    if row is None:
        return ""
    return (row["proxy_url"] or "").strip()


def _load_proxy_enabled() -> bool:
    db_path = current_db_path()
    if not db_path.exists():
        return False
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT proxy_enabled FROM settings WHERE rowid = 1"
        ).fetchone()
    except sqlite3.Error:
        # Column may not exist yet; fall back to old per-phase flags.
        return _load_proxy_enabled_legacy(conn)
    finally:
        conn.close()
    if row is None:
        return False
    return bool(row["proxy_enabled"])


def _load_proxy_enabled_legacy(conn: sqlite3.Connection) -> bool:
    """Fallback for DBs that haven't been migrated to proxy_enabled yet."""
    try:
        row = conn.execute(
            "SELECT proxy_bootstrap_enabled, proxy_explore_enabled FROM settings WHERE rowid = 1"
        ).fetchone()
    except sqlite3.Error:
        return False
    if row is None:
        return False
    return bool(row["proxy_bootstrap_enabled"]) or bool(row["proxy_explore_enabled"])
