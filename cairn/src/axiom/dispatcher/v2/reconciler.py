from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from axiom.dispatcher.v2.retry import ExploreRetryPolicy
from axiom.dispatcher.v2.schemas import DispatchAction, IntentSnapshot, TaskSnapshot


class Reconciler:
    def __init__(
        self,
        *,
        now: Callable[[], datetime] | None = None,
        explore_retry: ExploreRetryPolicy | None = None,
    ):
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.explore_retry = explore_retry or ExploreRetryPolicy()

    def calculate(self, tasks: list[TaskSnapshot]) -> list[DispatchAction]:
        actions: list[DispatchAction] = []
        for task in tasks:
            if task.status != "running":
                continue
            if task.phase == "bootstrap":
                bootstrap = next((item for item in task.intents if item.id == "i001"), None)
                if bootstrap and bootstrap.status == "pending" and self._eligible(bootstrap):
                    actions.append(
                        DispatchAction(kind="bootstrap", task_id=task.id, intent_id=bootstrap.id)
                    )
                elif bootstrap and bootstrap.status == "completed" and not task.reason_worker:
                    actions.append(DispatchAction(kind="reason", task_id=task.id))
                continue
            if task.phase == "reason":
                if task.reason_due and not task.reason_worker:
                    actions.append(DispatchAction(kind="reason", task_id=task.id))
                continue
            if task.phase == "explore":
                open_intents = [
                    item
                    for item in task.intents
                    if item.id != "i001"
                    and item.status in {"pending", "claimed", "running", "concluding"}
                ]
                pending = [item for item in open_intents if item.status == "pending"]
                for intent in pending:
                    if not self.explore_retry.can_attempt(intent.retry_count):
                        actions.append(
                            DispatchAction(
                                kind="needs_replan", task_id=task.id, intent_id=intent.id
                            )
                        )
                    elif self._eligible(intent):
                        actions.append(
                            DispatchAction(kind="explore", task_id=task.id, intent_id=intent.id)
                        )
                if not open_intents and not task.reason_worker:
                    actions.append(DispatchAction(kind="reason", task_id=task.id))
                continue
            if task.phase == "waiting" and task.reason_due and not task.reason_worker:
                actions.append(DispatchAction(kind="reason", task_id=task.id))
        return actions

    def _eligible(self, intent: IntentSnapshot) -> bool:
        if not intent.next_retry_at:
            return intent.last_failure_kind != "needs_hint"
        return _parse_time(intent.next_retry_at) <= self.now()


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
