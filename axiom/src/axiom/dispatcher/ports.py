from __future__ import annotations

from typing import Protocol

from axiom.dispatcher.schemas import ClaimedIntent, DispatcherSnapshot


class DispatchControlPlane(Protocol):
    """The only boundary the scheduler uses to touch durable state.

    Implementations may call the AXIOM API over HTTP or use an in-process
    application adapter.  The reconciliation algorithm never receives a
    database connection and therefore cannot accidentally bypass lifecycle
    checks, leases, or audit events.
    """

    def snapshot(self) -> DispatcherSnapshot:
        ...

    def claim(
        self,
        *,
        task_id: str,
        intent_id: str,
        worker_name: str,
    ) -> ClaimedIntent | None:
        """Atomically claim an intent, returning None on a stale conflict."""
