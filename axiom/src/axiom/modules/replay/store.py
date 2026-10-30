"""One request/response pair kept just long enough to be looked at.

The replay page is not a console: the reviewer sends a packet through the tool, gets a
URL back, opens it with playwright, and photographs what is on the page.  That page
therefore has to be a document, not an application — no state, no buttons, nothing to
click.  Which means the data has to live somewhere the page can read it once.

Ten minutes is the whole lifetime, because the data is only ever needed for the few
seconds between "send" and "screenshot".  Keeping it longer would mean holding live
session cookies and response bodies from a client engagement in memory for no reason, so
entries expire and are swept rather than persisted.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any


TTL_SECONDS = 600
# A reviewer works on one resulting step at a time; a couple of hundred entries covers
# a burst without letting a runaway loop grow the process.
MAX_ENTRIES = 200


@dataclass
class StoredExchange:
    key: str
    request_text: str
    response_text: str
    # Everything the page needs to draw its header line, and nothing it does not.
    summary: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def remaining(self, now: float | None = None) -> int:
        moment = now if now is not None else time.time()
        return max(0, int(self.created_at + TTL_SECONDS - moment))


class ExchangeStore:
    """Bounded, expiring, in-process.  Losing it on restart is acceptable by design."""

    def __init__(self, ttl_seconds: int = TTL_SECONDS, max_entries: int = MAX_ENTRIES):
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._items: dict[str, StoredExchange] = {}
        self._lock = threading.Lock()

    def put(
        self,
        *,
        request_text: str,
        response_text: str,
        summary: dict[str, Any] | None = None,
    ) -> StoredExchange:
        entry = StoredExchange(
            key=secrets.token_urlsafe(16),
            request_text=request_text,
            response_text=response_text,
            summary=summary or {},
        )
        with self._lock:
            self._sweep_locked()
            # Oldest first: a reviewer who queued ten packets is still working on the
            # last one, not the first.
            while len(self._items) >= self.max_entries:
                oldest = min(self._items.values(), key=lambda item: item.created_at)
                self._items.pop(oldest.key, None)
            self._items[entry.key] = entry
        return entry

    def get(self, key: str) -> StoredExchange | None:
        with self._lock:
            self._sweep_locked()
            return self._items.get(key)

    def purge(self) -> int:
        with self._lock:
            return self._sweep_locked()

    def stats(self) -> dict[str, int]:
        with self._lock:
            self._sweep_locked()
            return {"entries": len(self._items), "ttl_seconds": self.ttl_seconds}

    def _sweep_locked(self) -> int:
        now = time.time()
        expired = [
            key for key, item in self._items.items()
            if item.created_at + self.ttl_seconds <= now
        ]
        for key in expired:
            self._items.pop(key, None)
        return len(expired)


# One store per process: the service is a single uvicorn worker, and a second store
# would mean a URL that works only sometimes.
_STORE = ExchangeStore()


def store() -> ExchangeStore:
    return _STORE


__all__ = [
    "MAX_ENTRIES",
    "TTL_SECONDS",
    "ExchangeStore",
    "StoredExchange",
    "store",
]
