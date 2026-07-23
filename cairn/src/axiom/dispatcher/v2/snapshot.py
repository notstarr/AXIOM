from __future__ import annotations

import json
from collections import Counter

from sqlalchemy import Engine, and_, case, func, select

from axiom.dispatcher.v2.schemas import (
    DispatcherSnapshot,
    IntentSnapshot,
    TaskSnapshot,
    WorkerSnapshot,
)
from axiom.infrastructure.database.metadata import (
    dispatch_workers,
    context_revisions,
    intents,
    reason_workspaces,
    tasks,
    todos,
    worker_runtime_states,
)


class DispatcherSnapshotService:
    def __init__(self, engine: Engine):
        self.engine = engine

    def build(self) -> DispatcherSnapshot:
        with self.engine.connect() as conn:
            # A Reason round must consume the snapshot it was opened against,
            # not merely everything that finished before it committed. An
            # Explore Intent can finish while Reason is still working; using
            # the workspace's context revision as the watermark lets the next
            # round see that late result and prevents it from being lost.
            reason_cutoffs: dict[str, str] = {}
            last_reason_committed_at: dict[str, str] = {}
            cutoff_rows = conn.execute(
                select(
                    reason_workspaces.c.task_id,
                    reason_workspaces.c.committed_at,
                    case(
                        (
                            context_revisions.c.created_at
                            <= reason_workspaces.c.committed_at,
                            context_revisions.c.created_at,
                        ),
                        else_=reason_workspaces.c.committed_at,
                    ).label("context_created_at"),
                )
                .select_from(
                    reason_workspaces.join(
                        context_revisions,
                        and_(
                            context_revisions.c.task_id == reason_workspaces.c.task_id,
                            context_revisions.c.revision == reason_workspaces.c.context_revision,
                        ),
                    )
                )
                .where(
                    reason_workspaces.c.status == "committed",
                    reason_workspaces.c.committed_at.is_not(None),
                )
                .order_by(reason_workspaces.c.task_id, reason_workspaces.c.committed_at.desc())
            ).mappings()
            for row in cutoff_rows:
                reason_cutoffs.setdefault(row["task_id"], row["context_created_at"])
                last_reason_committed_at.setdefault(
                    row["task_id"], row["committed_at"]
                )

            intent_rows = conn.execute(select(intents)).mappings().all()
            intents_by_task: dict[str, list[IntentSnapshot]] = {}
            terminal_delta_times: dict[str, list[str]] = {}
            needs_replan_by_task: set[str] = set()
            open_explore_counts: Counter[str] = Counter()
            running_by_worker: Counter[str] = Counter()
            for row in intent_rows:
                snapshot = IntentSnapshot(
                    id=row["id"],
                    status=row["status"],
                    creator=row["creator"],
                    retry_count=row["retry_count"],
                    infrastructure_retry_count=row["infrastructure_retry_count"],
                    next_retry_at=row["next_retry_at"],
                    last_failure_kind=row["last_failure_kind"],
                    failure_class=row["failure_class"] or "execution",
                )
                intents_by_task.setdefault(row["task_id"], []).append(snapshot)
                cutoff = reason_cutoffs.get(row["task_id"])
                if (
                    not row["archived"]
                    and row["id"] != "i001"
                    and row["status"] in {
                        "pending",
                        "claimed",
                        "running",
                        "concluding",
                    }
                ):
                    open_explore_counts[row["task_id"]] += 1
                if (
                    not row["archived"]
                    and row["id"] != "i001"
                    and row["status"] in {"completed", "failed"}
                    and row["concluded_at"]
                    and (cutoff is None or row["concluded_at"] > cutoff)
                ):
                    terminal_delta_times.setdefault(row["task_id"], []).append(
                        row["concluded_at"]
                    )
                    if row["last_failure_kind"] == "retry_exhausted":
                        needs_replan_by_task.add(row["task_id"])
                if row["worker"] and row["status"] in {"claimed", "running", "concluding"}:
                    running_by_worker[row["worker"]] += 1

            task_rows = conn.execute(select(tasks).order_by(tasks.c.created_at, tasks.c.id)).mappings()
            todo_counts = {
                row["task_id"]: int(row["count"] or 0)
                for row in conn.execute(
                    select(todos.c.task_id, func.count().label("count"))
                    .where(todos.c.status.in_(["pending", "active"]))
                    .group_by(todos.c.task_id)
                ).mappings()
            }
            task_snapshots: list[TaskSnapshot] = []
            for row in task_rows:
                if row["reason_worker"]:
                    running_by_worker[row["reason_worker"]] += 1
                pending_todo_count = todo_counts.get(row["id"], 0)
                delta_times = terminal_delta_times.get(row["id"], [])
                terminal_delta_count = len(delta_times)
                task_snapshots.append(
                    TaskSnapshot(
                        id=row["id"],
                        status=row["status"],
                        phase=row["phase"],
                        reason_worker=row["reason_worker"],
                        # A waiting Task with unfinished Todos still has work
                        # for Reason. This is the recovery path for older
                        # workspaces that committed Todo updates without
                        # creating an Intent.
                        reason_due=(
                            row["phase"] == "reason"
                            or (
                                row["phase"] == "waiting"
                                and int(pending_todo_count or 0) > 0
                            )
                            or terminal_delta_count > 0
                        ),
                        terminal_delta_count=terminal_delta_count,
                        oldest_terminal_delta_at=min(delta_times) if delta_times else None,
                        last_reason_committed_at=last_reason_committed_at.get(row["id"]),
                        open_explore_intent_count=int(open_explore_counts[row["id"]]),
                        needs_replan_due=row["id"] in needs_replan_by_task,
                        pending_todo_count=int(pending_todo_count or 0),
                        worker_pool=json.loads(row["worker_pool_json"] or "[]"),
                        intents=intents_by_task.get(row["id"], []),
                    )
                )

            worker_rows = conn.execute(
                select(
                    dispatch_workers,
                    worker_runtime_states.c.cooldown_until,
                ).outerjoin(
                    worker_runtime_states,
                    worker_runtime_states.c.worker_name == dispatch_workers.c.name,
                )
            ).mappings()
            worker_snapshots = [
                WorkerSnapshot(
                    name=row["name"],
                    worker_type=row["worker_type"],
                    capabilities=json.loads(row["capabilities_json"]),
                    max_running=row["max_running"],
                    running=running_by_worker[row["name"]],
                    priority=row["priority"],
                    disabled=bool(row["disabled"]),
                    cooldown_until=row["cooldown_until"],
                    config=json.loads(row["config_json"] or "{}"),
                )
                for row in worker_rows
            ]
            return DispatcherSnapshot(tasks=task_snapshots, workers=worker_snapshots)
