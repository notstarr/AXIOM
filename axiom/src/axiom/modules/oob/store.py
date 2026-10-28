"""Out-of-band interaction capture for authorized red-team work.

Scope, deliberately narrow:

* exactly one endpoint per AXIOM instance, never per task;
* six-digit token in the path, so a payload URL is easy to paste anywhere;
* ten-minute lifetime, then the records wipe themselves;
* the lifecycle lives in this process's memory, not in PostgreSQL.

The last point is the design, not an oversight.  A ten-minute probe whose
records vanish on stop has no reason to survive a restart: persisting it would
mean a migration, a retention job and a cleanup path, all to protect data that
is supposed to expire anyway.  A restart clearing the bucket is the same
outcome as the timer firing.

Why one global endpoint instead of one per task: the callback itself carries
the identifying detail — source address, User-Agent, path label, timestamp.  A
per-task token would add bookkeeping without adding information.  Callers who
want attribution put it in the label segment (``/oob/123456/i052-ssrf``).
"""

from __future__ import annotations

import secrets
import threading
import time
from datetime import datetime, timezone

DEFAULT_TTL_SECONDS = 600
MAX_TTL_SECONDS = 3600
MAX_HITS = 500
MAX_BODY_BYTES = 2048
TOKEN_DIGITS = 6


def _now_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _new_token() -> str:
    """Six digits.  Not a credential — reads go through the authenticated API.

    The token only has to stop a stale callback from a previous bucket being
    mistaken for a fresh one, so guessability is irrelevant to confidentiality;
    anyone who guesses it can write junk into a throwaway list and read nothing.
    """

    return "".join(secrets.choice("0123456789") for _ in range(TOKEN_DIGITS))


class OobStore:
    """Single global capture bucket.

    Every method takes the lock.  The payload route is called from request
    handlers on the event loop's worker threads while the CLI can stop the
    bucket at any moment, so reads and the wipe must not interleave.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._token = ""
        self._base_url = ""
        self._started_at = 0.0
        self._expires_at = 0.0
        self._ttl = DEFAULT_TTL_SECONDS
        self._hits: list[dict] = []
        self._timer: threading.Timer | None = None

    # ---------------------------------------------------------- lifecycle
    def start(self, base_url: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> dict:
        """Open the bucket, or extend the one already open.

        Idempotent on purpose: the skill's first instruction is "check whether
        one is running and reuse it", and making ``start`` itself idempotent
        removes the need for the caller to branch.  Re-running it refreshes the
        window rather than minting a second token, so a URL already written
        into a target keeps working.
        """

        ttl = max(30, min(int(ttl_seconds or DEFAULT_TTL_SECONDS), MAX_TTL_SECONDS))
        with self._lock:
            now = time.time()
            if not self._token:
                self._token = _new_token()
                self._started_at = now
                self._hits = []
            self._base_url = base_url.rstrip("/")
            self._ttl = ttl
            self._expires_at = now + ttl
            self._arm_timer(ttl)
            return self._snapshot(now)

    def stop(self) -> dict:
        with self._lock:
            cleared = len(self._hits)
            was_running = bool(self._token)
            self._disarm_timer()
            self._token = ""
            self._base_url = ""
            self._started_at = 0.0
            self._expires_at = 0.0
            self._hits = []
            return {"stopped": was_running, "cleared": cleared}

    def status(self) -> dict:
        with self._lock:
            return self._snapshot(time.time())

    def hits(self, label: str | None = None, limit: int = 200) -> list[dict]:
        with self._lock:
            self._expire_if_due(time.time())
            rows = self._hits
            if label:
                needle = label.strip().lower()
                rows = [r for r in rows if needle in r["label"].lower()]
            # Newest first: the reason someone runs this is to see what just hit.
            # ``_sig`` is the dedup key, not part of the record.
            return [{k: v for k, v in r.items() if k != "_sig"} for r in reversed(rows[-limit:])]

    # ---------------------------------------------------------- recording
    def record(
        self,
        token: str,
        *,
        method: str,
        path: str,
        query: str,
        headers: dict[str, str],
        client: str,
        label: str = "",
        body: bytes = b"",
    ) -> bool:
        """Store one callback.  Returns False when the token is not live.

        A wrong token must be indistinguishable from a missing route, so the
        caller answers 404 either way and a prober learns nothing about whether
        a bucket happens to be open.
        """

        with self._lock:
            now = time.time()
            self._expire_if_due(now)
            if not self._token or not secrets.compare_digest(token, self._token):
                return False

            label = (label or "").strip("/")
            body_size = len(body)
            preview = body[:MAX_BODY_BYTES].decode("utf-8", "replace") if body else ""
            signature = (client, label, method, query, preview[:200])
            for row in self._hits:
                if row["_sig"] == signature:
                    row["count"] += 1
                    row["last_at"] = _now_iso(now)
                    return True
            if len(self._hits) >= MAX_HITS:
                # Oldest out.  A target in a retry loop must not be able to grow
                # this list without bound and take the process with it.
                self._hits.pop(0)
            self._hits.append({
                "_sig": signature,
                "at": _now_iso(now),
                "last_at": _now_iso(now),
                "count": 1,
                "client": client,
                "method": method,
                "path": path,
                "query": query,
                "label": label,
                "user_agent": headers.get("user-agent", ""),
                "host": headers.get("host", ""),
                "referer": headers.get("referer", ""),
                "body_size": body_size,
                "body_preview": preview,
            })
            return True

    # ---------------------------------------------------------- internals
    def _snapshot(self, now: float) -> dict:
        self._expire_if_due(now)
        running = bool(self._token)
        return {
            "running": running,
            "url": f"{self._base_url}/oob/{self._token}" if running else "",
            "token": self._token if running else "",
            "base_url": self._base_url if running else "",
            "started_at": _now_iso(self._started_at) if running else "",
            "expires_at": _now_iso(self._expires_at) if running else "",
            "remaining_seconds": max(0, int(self._expires_at - now)) if running else 0,
            "ttl_seconds": self._ttl if running else DEFAULT_TTL_SECONDS,
            "hit_count": sum(row["count"] for row in self._hits),
            "unique_count": len(self._hits),
        }

    def _expire_if_due(self, now: float) -> None:
        if self._token and now >= self._expires_at:
            self._disarm_timer()
            self._token = ""
            self._base_url = ""
            self._started_at = 0.0
            self._expires_at = 0.0
            self._hits = []

    def _arm_timer(self, ttl: int) -> None:
        self._disarm_timer()
        timer = threading.Timer(ttl, self._on_timeout)
        timer.daemon = True
        self._timer = timer
        timer.start()

    def _disarm_timer(self) -> None:
        timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()

    def _on_timeout(self) -> None:
        # Belt and braces: the timer wipes, and every read re-checks the clock.
        # A timer can land late under load, and a caller must never see an
        # expired bucket as still open.
        with self._lock:
            self._expire_if_due(time.time())


STORE = OobStore()
"""Process-wide singleton: the product requirement is exactly one bucket."""


def public_base_url() -> str:
    """Where the public payload URL points.

    Configurable because the API answers on 127.0.0.1 behind Caddy and has no
    idea what address the outside world uses; guessing from the request would
    hand out ``127.0.0.1`` in payloads that are meant for someone else.
    """

    import os

    return os.environ.get("AXIOM_OOB_BASE_URL", "http://127.0.0.1:8889").rstrip("/")
