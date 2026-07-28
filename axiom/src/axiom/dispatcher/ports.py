from __future__ import annotations

from typing import Protocol

from axiom.dispatcher.schemas import ClaimAction, ClaimedWork, DispatcherSnapshot
from axiom.modules.tasks.schemas import RunDeadlineReconcileReport


class DispatchControlPlane(Protocol):
    """The only boundary the scheduler uses to touch durable state.

    Implementations may call the AXIOM API over HTTP or use an in-process
    application adapter.  The reconciliation algorithm never receives a
    database connection and therefore cannot accidentally bypass lifecycle
    checks, leases, or audit events.
    """

    def snapshot(self) -> DispatcherSnapshot:
        ...

    def reconcile_task_deadlines(self) -> RunDeadlineReconcileReport:
        ...

    def claim_work(self, action: ClaimAction) -> ClaimedWork | None:
        """Atomically claim work, returning None on a stale conflict."""
