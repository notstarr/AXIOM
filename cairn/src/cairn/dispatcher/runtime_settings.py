from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

from cairn.server.db import current_db_path

LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    intent_timeout: int
    reason_timeout: int
    bootstrap_worker_count: int
    explore_worker_count: int
    reason_worker_count: int


def load_runtime_settings() -> RuntimeSettings | None:
    db_path = current_db_path()
    if not db_path.exists():
        return None
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT intent_timeout, reason_timeout,
                bootstrap_worker_count, explore_worker_count, reason_worker_count
            FROM settings
            WHERE rowid = 1
            """
        ).fetchone()
    except sqlite3.Error as exc:
        LOG.warning("failed to read runtime settings error=%s", exc)
        return None
    finally:
        conn.close()
    if row is None:
        return None
    return RuntimeSettings(
        intent_timeout=max(5, int(row["intent_timeout"] or 5)),
        reason_timeout=max(5, int(row["reason_timeout"] or 5)),
        bootstrap_worker_count=max(1, int(row["bootstrap_worker_count"] or 1)),
        explore_worker_count=max(1, int(row["explore_worker_count"] or 1)),
        reason_worker_count=max(1, int(row["reason_worker_count"] or 1)),
    )
