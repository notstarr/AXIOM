from __future__ import annotations

import json
import os
import shutil
import threading
import uuid
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, select, union

from axiom.modules.context.repository import ContextRepository
from axiom.modules.context.tables import context_revisions
from axiom.modules.graph.tables import intents
from axiom.modules.plan.tables import plan_runs
from axiom.modules.reason.tables import reason_runs
from axiom.modules.vulnerability_reviews.tables import vulnerability_review_runs
from axiom.shared.clock import parse_utc, utcnow
from axiom.shared.database import transaction
from axiom.shared.storage import FileStore


ACTIVE_STATUSES = frozenset({"claimed", "running", "concluding"})
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
OWNER_TABLES = {
    "explore": intents,
    "reason": reason_runs,
    "plan": plan_runs,
    "review": vulnerability_review_runs,
}
OWNER_COLUMNS = {
    "explore": "intent_id",
    "reason": "reason_run_id",
    "plan": "plan_run_id",
    "review": "review_run_id",
}


@dataclass(frozen=True, slots=True)
class RetentionConfig:
    enabled: bool = False
    grace_days: int = 7
    keep_revisions: int = 3
    hot_window_minutes: int = 30
    orphan_grace_hours: int = 24
    max_revisions: int = 100
    max_bytes: int = 2 * 1024 * 1024 * 1024

    @classmethod
    def from_env(cls) -> "RetentionConfig":
        def integer(name: str, default: int, minimum: int = 0) -> int:
            try:
                return max(minimum, int(os.getenv(name, str(default))))
            except ValueError:
                return default

        return cls(
            enabled=os.getenv("AXIOM_CONTEXT_GC_ENABLED", "0").strip().lower()
            in {"1", "true", "yes", "on"},
            grace_days=integer("AXIOM_CONTEXT_GC_GRACE_DAYS", 7),
            keep_revisions=integer("AXIOM_CONTEXT_GC_KEEP_REVISIONS", 3),
            hot_window_minutes=integer("AXIOM_CONTEXT_GC_HOT_WINDOW_MINUTES", 30),
            orphan_grace_hours=integer("AXIOM_CONTEXT_GC_ORPHAN_GRACE_HOURS", 24),
            max_revisions=integer("AXIOM_CONTEXT_GC_MAX_REVISIONS", 100, 1),
            max_bytes=integer("AXIOM_CONTEXT_GC_MAX_BYTES", 2 * 1024 * 1024 * 1024, 1),
        )


@dataclass(frozen=True, slots=True)
class RetentionTarget:
    path: str
    task_id: str
    revision: int | None
    bytes: int
    kind: str
    reason: str
    phase: str | None = None


@dataclass(frozen=True, slots=True)
class RetentionPlan:
    generated_at: str
    targets: list[RetentionTarget]
    database_records: int
    reclaimable_count: int
    protected_count: int
    orphan_count: int
    total_bytes: int


@dataclass(frozen=True, slots=True)
class RetentionRunResult:
    generated_at: str
    dry_run: bool
    paused: bool
    removed: list[RetentionTarget]
    skipped: list[dict[str, str]]
    freed_bytes: int
    protected_count: int
    orphan_count: int


_RUN_LOCK = threading.Lock()
LOGGER = logging.getLogger(__name__)


def _tree_size(path: Path) -> int:
    total = 0
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    if not path.is_dir():
        return 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
    return total


class ContextRetentionService:
    """Revision-level Context garbage collection with a dry-run plan."""

    def __init__(self, engine: Engine, store: FileStore, config: RetentionConfig | None = None):
        self.engine = engine
        self.store = store
        self.config = config or RetentionConfig.from_env()
        self.repository = ContextRepository()
        self._paused = False
        self._state_lock = threading.Lock()

    def pause(self) -> None:
        with self._state_lock:
            self._paused = True

    def resume(self) -> None:
        with self._state_lock:
            self._paused = False

    @property
    def paused(self) -> bool:
        with self._state_lock:
            return self._paused

    def plan(self) -> RetentionPlan:
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=self.config.grace_days)
        orphan_cutoff = now - timedelta(hours=self.config.orphan_grace_hours)
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(context_revisions).where(
                    context_revisions.c.storage_state == "present"
                )
            ).mappings().all()
            active_leases = self._active_leases(conn)
            active_task_ids = self._active_task_ids(conn)
            owner_rows = self._owners(conn, rows)

        latest: set[tuple[str, str]] = set()
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault((row["task_id"], row["phase"]), []).append(row)
        for key, group in grouped.items():
            group.sort(key=lambda item: (str(item["created_at"]), int(item["revision"])), reverse=True)
            latest.update((key[0], str(item["revision"])) for item in group[: self.config.keep_revisions])

        targets: list[RetentionTarget] = []
        protected = 0
        for row in rows:
            task_id = str(row["task_id"])
            revision = int(row["revision"])
            path = self._revision_dir(row["manifest_key"])
            if path is None:
                protected += 1
                continue
            if (task_id, str(revision)) in latest:
                protected += 1
                continue
            if self._is_hot(path, now):
                protected += 1
                continue
            owner = self._owner_for_revision(row, owner_rows)
            if row["lease_id"] in active_leases or (owner and owner["status"] in ACTIVE_STATUSES):
                protected += 1
                continue
            created_at = self._parse_time(row["created_at"])
            if created_at > cutoff:
                protected += 1
                continue
            if owner and owner["status"] not in TERMINAL_STATUSES:
                # A pending owner may receive a new lease, but this old lease
                # snapshot is stale once the grace window has elapsed.
                if owner["status"] != "pending" or owner.get("lease_id") == row["lease_id"]:
                    protected += 1
                    continue
                reason = "stale_pending_lease"
            else:
                reason = "terminal_owner" if owner else "unowned_revision"
            targets.append(
                RetentionTarget(
                    path=path.relative_to(self.store.root).as_posix(),
                    task_id=task_id,
                    revision=revision,
                    bytes=_tree_size(path),
                    kind="revision",
                    reason=reason,
                    phase=str(row["phase"]),
                )
            )

        db_paths = {self._revision_dir(row["manifest_key"]) for row in rows}
        orphan_root = self.store.root / "contexts"
        orphan_count = 0
        if orphan_root.is_dir():
            for task_dir in orphan_root.iterdir():
                if not task_dir.is_dir() or task_dir.is_symlink():
                    continue
                for revision_dir in task_dir.iterdir():
                    if not revision_dir.is_dir() or revision_dir.is_symlink():
                        continue
                    if (
                        revision_dir.resolve() in db_paths
                        or task_dir.name in active_task_ids
                        or self._is_hot(revision_dir, now)
                    ):
                        continue
                    try:
                        if revision_dir.stat().st_mtime > orphan_cutoff.timestamp():
                            continue
                    except OSError:
                        continue
                    try:
                        revision = int(revision_dir.name[1:]) if revision_dir.name.startswith("r") else None
                    except ValueError:
                        revision = None
                    orphan_count += 1
                    targets.append(
                        RetentionTarget(
                            path=revision_dir.relative_to(self.store.root).as_posix(),
                            task_id=task_dir.name,
                            revision=revision,
                            bytes=_tree_size(revision_dir),
                            kind="orphan",
                            reason="missing_context_revision",
                        )
                    )

        # Clear orphan failures first, then reclaim the largest cold snapshots
        # within the fixed batch budget so each run produces useful disk relief.
        targets.sort(key=lambda item: (item.kind != "orphan", -item.bytes, item.path))
        return RetentionPlan(
            generated_at=utcnow(),
            targets=targets,
            database_records=len(rows),
            reclaimable_count=len(targets),
            protected_count=protected,
            orphan_count=orphan_count,
            total_bytes=sum(item.bytes for item in targets),
        )

    def run(
        self,
        *,
        dry_run: bool = False,
        actor: str = "system.context-gc",
        max_revisions: int | None = None,
        max_bytes: int | None = None,
    ) -> RetentionRunResult:
        plan = self.plan()
        revision_limit = (
            max(1, min(self.config.max_revisions, max_revisions))
            if max_revisions is not None
            else self.config.max_revisions
        )
        byte_limit = (
            max(1, min(self.config.max_bytes, max_bytes))
            if max_bytes is not None
            else self.config.max_bytes
        )
        selected: list[RetentionTarget] = []
        selected_bytes = 0
        for target in plan.targets:
            if len(selected) >= revision_limit or selected_bytes + target.bytes > byte_limit:
                break
            selected.append(target)
            selected_bytes += target.bytes
        if dry_run:
            return RetentionRunResult(
                generated_at=plan.generated_at,
                dry_run=True,
                paused=self.paused or not self.config.enabled,
                removed=selected,
                skipped=[
                    {"path": target.path, "reason": "batch limit"}
                    for target in plan.targets[len(selected):]
                ],
                freed_bytes=selected_bytes,
                protected_count=plan.protected_count,
                orphan_count=plan.orphan_count,
            )
        if self.paused or not self.config.enabled:
            return RetentionRunResult(
                generated_at=plan.generated_at,
                dry_run=False,
                paused=True,
                removed=[],
                skipped=[{"path": "", "reason": "context retention is paused"}],
                freed_bytes=0,
                protected_count=plan.protected_count,
                orphan_count=plan.orphan_count,
            )
        if not _RUN_LOCK.acquire(blocking=False):
            return RetentionRunResult(
                generated_at=plan.generated_at,
                dry_run=False,
                paused=False,
                removed=[],
                skipped=[{"path": "", "reason": "another retention run is active"}],
                freed_bytes=0,
                protected_count=plan.protected_count,
                orphan_count=plan.orphan_count,
            )
        removed: list[RetentionTarget] = []
        skipped: list[dict[str, str]] = []
        freed = 0
        try:
            self._reconcile_quarantine()
            for target in plan.targets:
                if len(removed) >= revision_limit or freed + target.bytes > byte_limit:
                    skipped.append({"path": target.path, "reason": "batch limit"})
                    continue
                result = self._reclaim(target)
                if result is None:
                    skipped.append({"path": target.path, "reason": "became protected or disappeared"})
                    continue
                removed.append(target)
                freed += result
        finally:
            _RUN_LOCK.release()
        return RetentionRunResult(
            generated_at=plan.generated_at,
            dry_run=False,
            paused=False,
            removed=removed,
            skipped=skipped,
            freed_bytes=freed,
            protected_count=plan.protected_count,
            orphan_count=plan.orphan_count,
        )

    def _reclaim(self, target: RetentionTarget) -> int | None:
        source = (self.store.root / target.path).resolve()
        contexts_root = (self.store.root / "contexts").resolve()
        if contexts_root not in source.parents or not source.is_dir():
            return None
        quarantine_root = self.store.root / ".staging" / "context-gc"
        quarantine_root.mkdir(parents=True, exist_ok=True)
        destination = quarantine_root / f"{target.task_id}-{target.revision or 'orphan'}-{uuid.uuid4().hex}"
        size = _tree_size(source)
        marked = False
        try:
            os.replace(source, destination)
            metadata = destination / ".context-gc.json"
            metadata.write_text(
                json.dumps(
                    {
                        "path": target.path,
                        "task_id": target.task_id,
                        "revision": target.revision,
                        "kind": target.kind,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            if target.kind == "revision" and target.revision is not None:
                with transaction(self.engine) as conn:
                    row = conn.execute(
                        select(context_revisions)
                        .where(
                            context_revisions.c.task_id == target.task_id,
                            context_revisions.c.revision == target.revision,
                        )
                        .with_for_update()
                    ).mappings().one_or_none()
                    if row is None or row["storage_state"] != "present":
                        raise RuntimeError("revision state changed")
                    updated = self.repository.mark_reclaimed(
                        conn, target.task_id, target.revision, utcnow()
                    )
                    if not updated:
                        raise RuntimeError("revision state changed before reclaim mark")
                marked = True
            try:
                shutil.rmtree(destination, ignore_errors=False)
            except OSError:
                if marked:
                    LOGGER.warning("Context quarantine cleanup deferred: %s", destination)
                    return size
                raise
            return size
        except Exception:
            if destination.exists() and not source.exists() and not marked:
                source.parent.mkdir(parents=True, exist_ok=True)
                try:
                    (destination / ".context-gc.json").unlink(missing_ok=True)
                    os.replace(destination, source)
                except OSError:
                    # The quarantine directory is intentionally left for a
                    # later reconciliation pass rather than risking data loss.
                    pass
            return None

    def _reconcile_quarantine(self) -> None:
        """Recover quarantine entries left by an interrupted reclaim.

        The metadata file is written only after the atomic move.  A present
        database row is restored to its original path; reclaimed/orphan rows
        are safe to discard.  Filesystem operations happen outside the short
        metadata read transaction.
        """
        root = self.store.root / ".staging" / "context-gc"
        if not root.is_dir():
            return
        entries: list[tuple[Path, dict[str, Any]]] = []
        for entry in root.iterdir():
            if not entry.is_dir() or entry.is_symlink():
                continue
            try:
                metadata = json.loads((entry / ".context-gc.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(metadata, dict) and isinstance(metadata.get("path"), str):
                entries.append((entry, metadata))
        if not entries:
            return
        revisions = [
            (item[1].get("task_id"), item[1].get("revision"))
            for item in entries
            if item[1].get("kind") == "revision"
        ]
        states: dict[tuple[str, int], str] = {}
        if revisions:
            with self.engine.connect() as conn:
                for task_id, revision in revisions:
                    if not task_id or not isinstance(revision, int):
                        continue
                    row = conn.execute(
                        select(context_revisions.c.storage_state).where(
                            context_revisions.c.task_id == task_id,
                            context_revisions.c.revision == revision,
                        )
                    ).scalar_one_or_none()
                    if row is not None:
                        states[(str(task_id), revision)] = str(row)
        for entry, metadata in entries:
            kind = metadata.get("kind")
            task_id = str(metadata.get("task_id") or "")
            revision = metadata.get("revision")
            state = states.get((task_id, revision)) if isinstance(revision, int) else None
            if kind == "revision" and state == "present":
                source = (self.store.root / metadata["path"]).resolve()
                contexts_root = (self.store.root / "contexts").resolve()
                if contexts_root not in source.parents:
                    LOGGER.warning("Ignoring invalid Context quarantine path: %s", source)
                    continue
                if not source.exists():
                    source.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        os.replace(entry, source)
                    except OSError:
                        LOGGER.warning("Context quarantine restore deferred: %s", entry)
                continue
            try:
                shutil.rmtree(entry)
            except OSError:
                LOGGER.warning("Context quarantine cleanup deferred: %s", entry)

    @staticmethod
    def _parse_time(value: Any) -> datetime:
        if isinstance(value, datetime):
            parsed = value
        else:
            parsed = parse_utc(str(value))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    @staticmethod
    def _owners(conn, rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
        result: dict[tuple[str, str], dict[str, Any]] = {}
        for phase, table in OWNER_TABLES.items():
            column = OWNER_COLUMNS[phase]
            ids = {str(item[column]) for item in rows if item.get(column)}
            if not ids:
                continue
            for row in conn.execute(select(table).where(table.c.id.in_(ids))).mappings():
                result[(column, str(row["id"]))] = dict(row)
        return result

    @staticmethod
    def _owner_for_revision(
        row: Any, owners: dict[tuple[str, str], dict[str, Any]]
    ) -> dict[str, Any] | None:
        for column in OWNER_COLUMNS.values():
            value = row.get(column)
            if value:
                return owners.get((column, str(value)))
        return None

    @staticmethod
    def _active_leases(conn) -> set[str]:
        queries = []
        for table in OWNER_TABLES.values():
            queries.append(
                select(table.c.lease_id).where(
                    table.c.lease_id.is_not(None), table.c.status.in_(ACTIVE_STATUSES)
                )
            )
        query = union(*queries)
        return {str(item) for item in conn.execute(query).scalars().all() if item}

    @staticmethod
    def _active_task_ids(conn) -> set[str]:
        queries = []
        for table in OWNER_TABLES.values():
            queries.append(select(table.c.task_id).where(table.c.status.in_(ACTIVE_STATUSES)))
        query = union(*queries)
        return {str(item) for item in conn.execute(query).scalars().all() if item}

    def _revision_dir(self, manifest_key: str) -> Path | None:
        path = (self.store.root / manifest_key).resolve()
        contexts_root = (self.store.root / "contexts").resolve()
        if path.name != "manifest.json" or contexts_root not in path.parents:
            return None
        revision_dir = path.parent
        if revision_dir.parent.parent != contexts_root:
            return None
        return revision_dir

    def _is_hot(self, path: Path, now: datetime) -> bool:
        cutoff = now.timestamp() - self.config.hot_window_minutes * 60
        try:
            if path.stat().st_mtime >= cutoff:
                return True
        except OSError:
            return False
        stack = [path]
        while stack:
            current = stack.pop()
            try:
                entries = list(os.scandir(current))
            except OSError:
                continue
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False) and entry.stat(follow_symlinks=False).st_mtime >= cutoff:
                        return True
                except OSError:
                    continue
        return False


__all__ = [
    "ContextRetentionService",
    "RetentionConfig",
    "RetentionPlan",
    "RetentionRunResult",
    "RetentionTarget",
]
