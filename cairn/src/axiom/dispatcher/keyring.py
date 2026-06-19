from __future__ import annotations

import logging
import sqlite3

from axiom.shared.keyring import KeyringError, decrypt_secret
from axiom.server.db import current_db_path

LOG = logging.getLogger(__name__)


def load_provider_api_key(provider: str) -> str | None:
    db_path = current_db_path()
    if not db_path.exists():
        return None
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT key_ciphertext, is_active
            FROM provider_keys
            WHERE provider = ?
            """,
            (provider,),
        ).fetchone()
    except sqlite3.Error as exc:
        LOG.warning("failed to read provider key provider=%s error=%s", provider, exc)
        return None
    finally:
        conn.close()

    if row is None or not bool(row["is_active"]):
        return None
    ciphertext = row["key_ciphertext"]
    if not isinstance(ciphertext, str) or not ciphertext:
        return None
    try:
        return decrypt_secret(ciphertext)
    except KeyringError as exc:
        LOG.warning("failed to decrypt provider key provider=%s error=%s", provider, exc)
        return None

