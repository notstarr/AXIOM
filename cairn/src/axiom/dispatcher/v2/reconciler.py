from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from axiom.dispatcher.v2.retry import ExploreRetryPolicy
from axiom.dispatcher.v2.schemas import DispatchAction, IntentSnapshot, TaskSnapshot
from axiom.modules.settings.schemas import RuntimeConfig


class Reconciler:
    def __init__(
        self,
        *,
        now: Callable[[], datetime] | None = None,
        explore_retry: ExploreRetryPolicy | None = None,
        runtime: RuntimeConfig | None = None,
    ):
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.explore_retry = explore_retry or ExploreRetryPolicy()
        self.runtime = runtime or RuntimeConfig()

    def calculate(
        self,
        tasks: list[TaskSnapshot],
        runtime: RuntimeConfig | None = None,
    ) -> list[DispatchAction]:
        runtime = runtime or self.runtime
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
                if (
                    task.reason_due
                    and not task.reason_worker
                    and self._minimum_reason_interval_elapsed(task, runtime)
                ):
                    actions.append(DispatchAction(kind="reason", task_id=task.id))
                actions.extend(self._pending_explore_actions(task))
                continue
            if task.phase == "explore":
                actions.extend(self._pending_explore_actions(task))
                open_intents = self._open_explore_intents(task)
                # Incremental Reason consumes result batches instead of every
                # single Fact.  It still wakes early when the executable queue
                # is nearly drained, a result has waited too long, or an Intent
                # exhausted its retry budget and explicitly needs replanning.
                if self._reason_batch_due(task, runtime) and not task.reason_worker:
                    actions.append(DispatchAction(kind="reason", task_id=task.id))
                if (
                    not open_intents
                    and not task.reason_worker
                    and not task.last_reason_committed_at
                ):
                    if not any(
                        item.kind == "reason" and item.task_id == task.id for item in actions
                    ):
                        actions.append(DispatchAction(kind="reason", task_id=task.id))
                continue
            if (
                task.phase == "waiting"
                and task.pending_todo_count > 0
                and not task.reason_worker
                and self._minimum_reason_interval_elapsed(task, runtime)
            ):
                actions.append(DispatchAction(kind="reason", task_id=task.id))
        return actions

    def _reason_batch_due(self, task: TaskSnapshot, runtime: RuntimeConfig) -> bool:
        if not self._minimum_reason_interval_elapsed(task, runtime):
            return False
        # Compatibility for pre-watermark snapshots and migrated Tasks that
        # have never committed a Reason Workspace.
        has_delta = task.terminal_delta_count > 0 or (
            task.reason_due and not task.last_reason_committed_at
        )
        if not has_delta:
            return False
        if (
            task.reason_due
            and task.terminal_delta_count == 0
            and not task.last_reason_committed_at
        ):
            return True
        if task.needs_replan_due:
            return True
        if task.terminal_delta_count >= runtime.reason_batch_size:
            return True
        open_count = task.open_explore_intent_count
        if open_count == 0:
            open_count = len(self._open_explore_intents(task))
        if open_count <= runtime.reason_low_watermark:
            return True
        if task.oldest_terminal_delta_at:
            age = (self.now() - _parse_time(task.oldest_terminal_delta_at)).total_seconds()
            if age >= runtime.reason_max_wait_seconds:
                return True
        return False

    def _minimum_reason_interval_elapsed(
        self,
        task: TaskSnapshot,
        runtime: RuntimeConfig,
    ) -> bool:
        if not task.last_reason_committed_at or runtime.reason_min_interval_seconds <= 0:
            return True
        elapsed = (self.now() - _parse_time(task.last_reason_committed_at)).total_seconds()
        return elapsed >= runtime.reason_min_interval_seconds

    def _pending_explore_actions(self, task: TaskSnapshot) -> list[DispatchAction]:
        actions: list[DispatchAction] = []
        for intent in self._open_explore_intents(task):
            if intent.status != "pending":
                continue
            if not self.explore_retry.can_attempt(
                intent.retry_count,
                failure_class=intent.failure_class or "execution",
                infrastructure_retry_count=intent.infrastructure_retry_count,
            ):
                actions.append(
                    DispatchAction(kind="needs_replan", task_id=task.id, intent_id=intent.id)
                )
            elif self._eligible(intent):
                actions.append(
                    DispatchAction(kind="explore", task_id=task.id, intent_id=intent.id)
                )
        return actions

    @staticmethod
    def _open_explore_intents(task: TaskSnapshot) -> list[IntentSnapshot]:
        return [
            item
            for item in task.intents
            if item.id != "i001"
            and item.status in {"pending", "claimed", "running", "concluding"}
        ]

    def _eligible(self, intent: IntentSnapshot) -> bool:
        if not intent.next_retry_at:
            return intent.last_failure_kind != "needs_hint"
        return _parse_time(intent.next_retry_at) <= self.now()


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
