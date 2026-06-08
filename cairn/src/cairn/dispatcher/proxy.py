from __future__ import annotations

import logging
import sqlite3

from cairn.server.db import DEFAULT_DB

LOG = logging.getLogger(__name__)


def proxy_env_for_phase(phase: str) -> dict[str, str]:
    settings = _load_proxy_settings()
    if settings is None:
        return {}
    proxy_url, bootstrap_enabled, explore_enabled = settings
    if not proxy_url:
        return {}
    use_proxy = False
    if phase.startswith("bootstrap"):
        use_proxy = bootstrap_enabled
    elif phase.startswith("explore"):
        use_proxy = explore_enabled
    if not use_proxy:
        return {}
    return {
        "ALL_PROXY": proxy_url,
        "all_proxy": proxy_url,
        "HTTP_PROXY": proxy_url,
        "HTTPS_PROXY": proxy_url,
    }


def _load_proxy_settings() -> tuple[str, bool, bool] | None:
    db_path = DEFAULT_DB
    if not db_path.exists():
        return None
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT proxy_url, proxy_bootstrap_enabled, proxy_explore_enabled
            FROM settings
            WHERE rowid = 1
            """
        ).fetchone()
    except sqlite3.Error as exc:
        LOG.warning("failed to read proxy settings error=%s", exc)
        return None
    finally:
        conn.close()
    if row is None:
        return None
    return (
        (row["proxy_url"] or "").strip(),
        bool(row["proxy_bootstrap_enabled"]),
        bool(row["proxy_explore_enabled"]),
    )
