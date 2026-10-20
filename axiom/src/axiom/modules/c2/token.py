"""Per-listener C2 token derivation.

The HTTP beacon listener is reachable from the internet, so ``sid`` alone is
not authentication.  Derive a stable token per listener from the control-plane
secret; the oneliner carries it and the listener compares it in constant time.

Prefer AXIOM_C2_TOKEN_SECRET, then AXIOM_SERVICE_TOKEN, then the data-dir
service_token/auth_secret.key.  The process-local fallback keeps tests and
unconfigured local runs working, but a process restart invalidates old
oneliners; production deployments have AXIOM_SERVICE_TOKEN in the environment.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from pathlib import Path

logger = logging.getLogger(__name__)

_FALLBACK_SECRET = secrets.token_bytes(32)
_WARNED = False


def _secret() -> bytes:
    for name in ("AXIOM_C2_TOKEN_SECRET", "AXIOM_SERVICE_TOKEN"):
        value = os.environ.get(name, "").strip()
        if value:
            return value.encode("utf-8")

    data_dir = Path(os.environ.get("AXIOM_DATA_DIR", "~/.local/share/axiom-v3")).expanduser()
    for name in ("service_token", "auth_secret.key"):
        try:
            value = (data_dir / name).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            return value.encode("utf-8")

    global _WARNED
    if not _WARNED:
        logger.warning(
            "C2 listener token secret is not configured; using a process-local fallback"
        )
        _WARNED = True
    return _FALLBACK_SECRET


def c2_listener_token(listener_id: str) -> str:
    """Return the stable token that authorizes one HTTP C2 listener."""

    return hmac.new(
        _secret(),
        f"c2-listener:{listener_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:32]
