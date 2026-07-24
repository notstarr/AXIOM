from __future__ import annotations

from axiom.dispatcher.ports import DispatchControlPlane
from axiom.dispatcher.reconciler import Reconciler
from axiom.dispatcher.schemas import DispatchReport, DispatcherSnapshot


class Dispatcher:
    """One idempotent reconciliation tick.

    The loop itself can be driven by a process supervisor, a CLI command, or a
    long-running service.  Keeping one tick synchronous makes shutdown and
    failure handling explicit instead of hiding them inside an unbounded task.
    """

    def __init__(self, control_plane: DispatchControlPlane, *, reconciler: Reconciler | None = None):
        self.control_plane = control_plane
        self.reconciler = reconciler or Reconciler()

    def tick(
        self,
        *,
        max_claims: int | None = None,
        snapshot: DispatcherSnapshot | None = None,
    ) -> DispatchReport:
        snapshot = snapshot or self.control_plane.snapshot()
        actions = self.reconciler.calculate(snapshot)
        if max_claims is not None:
            if max_claims < 0:
                raise ValueError("max_claims must not be negative")
            actions = actions[:max_claims]
        report = DispatchReport(considered=self._pending_count(snapshot), planned=len(actions))
        for action in actions:
            claim = self.control_plane.claim(
                task_id=action.claim.task_id,
                intent_id=action.claim.intent_id,
                worker_name=action.claim.worker_name,
            )
            if claim is not None:
                report.claimed += 1
                report.claims.append(claim)
            else:
                report.conflicts += 1
        return report

    @staticmethod
    def _pending_count(snapshot) -> int:
        return sum(
            1
            for task in snapshot.tasks
            if task.status == "running"
            for intent in task.intents
            if intent.status == "pending"
        )
