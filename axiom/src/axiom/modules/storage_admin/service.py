from __future__ import annotations

import gzip
import json
import logging
import os
import shutil
import threading
import time
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import Engine, text

from axiom.modules.audit.schemas import RecordAuditEvent
from axiom.modules.audit.service import AuditService
from axiom.shared.clock import utcnow
from axiom.shared.storage import FileStore

from .schemas import (
    DatabaseStats,
    DatabaseTableStat,
    DiskStats,
    LogFileStat,
    LogRotateItem,
    LogRotateResult,
    LogStats,
    StorageCategory,
    StorageOverview,
)

LOGGER = logging.getLogger(__name__)

# MVP 固定保留策略；将来需要可调时再迁入 RuntimeConfig。
LOG_ARCHIVE_KEEP_DAYS = 30
AUTO_ROTATE_INTERVAL = timedelta(hours=24)
ARCHIVE_SUFFIX = ".log.gz"
AUTO_ROTATE_ACTOR = "system.storage-auto"

# 计入分类盘点的业务目录。`.staging` 等内部临时目录不单列，
# 它们的体积已包含在 total_bytes 与各分类差值中。
CATEGORY_DIRS = (
    "runs",
    "contexts",
    "tasks",
    "logs",
    "backups",
    "postgresql-migration",
    "postgresql-backups",
    "maintenance",
    "skills",
    "projects",
    "output",
)


def _tree_stats(root: Path) -> tuple[int, int]:
    """Return (bytes, file_count) below root, ignoring symlinks and unreadable entries."""
    size_bytes = 0
    items = 0
    stack = [str(root)]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                elif entry.is_file(follow_symlinks=False):
                    size_bytes += entry.stat(follow_symlinks=False).st_size
                    items += 1
            except OSError:
                continue
    return size_bytes, items


def _iter_files(root: Path) -> Iterator[Path]:
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                stack.append(Path(entry.path))
            elif entry.is_file(follow_symlinks=False):
                yield Path(entry.path)


class StorageAdminService:
    """只读存储盘点 + 日志 copytruncate 轮转。

    server 与 runner 的 stdout/stderr 由 launchd 重定向到 data_dir/logs/*.log，
    传统改名式轮转会把后续写入留在旧 inode 上，因此这里统一 copytruncate：
    复制 → gzip 校验 → 截断原文件，进程无需感知。
    自动轮转挂在 overview 的惰性检查上（超过间隔就顺手执行一次），
    单进程 launchd 托管下没有并发竞争，也避免引入定时线程。
    """

    def __init__(self, engine: Engine, store: FileStore):
        self.engine = engine
        self._root = store.root
        self._audit = AuditService(engine)
        self._rotate_lock = threading.Lock()

    # ---------- 盘点 ----------

    def overview(self) -> StorageOverview:
        self._auto_rotate_if_due()

        categories: list[StorageCategory] = []
        for rel in CATEGORY_DIRS:
            path = self._root / rel
            if not path.is_dir():
                continue
            size_bytes, items = _tree_stats(path)
            categories.append(
                StorageCategory(key=rel, path=str(path), bytes=size_bytes, items=items)
            )
        categories.sort(key=lambda item: item.bytes, reverse=True)

        disk_total, _, disk_free = shutil.disk_usage(self._root)
        return StorageOverview(
            generated_at=utcnow(),
            data_dir=str(self._root),
            total_bytes=_tree_stats(self._root)[0],
            disk=DiskStats(total_bytes=disk_total, free_bytes=disk_free),
            categories=categories,
            database=self._database_stats(),
            logs=self._log_stats(),
        )

    def _database_stats(self) -> DatabaseStats:
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT c.relname AS name,
                           pg_total_relation_size(c.oid) AS total_bytes,
                           GREATEST(COALESCE(s.n_live_tup, 0), 0) AS rows
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
                    WHERE n.nspname = 'axiom' AND c.relkind = 'r'
                    ORDER BY pg_total_relation_size(c.oid) DESC
                    LIMIT 10
                    """
                )
            ).mappings().all()
            total = conn.execute(
                text("SELECT pg_database_size(current_database())")
            ).scalar_one()
        return DatabaseStats(
            total_bytes=int(total),
            tables=[
                DatabaseTableStat(
                    name=row["name"],
                    total_bytes=int(row["total_bytes"]),
                    rows=int(row["rows"]),
                )
                for row in rows
            ],
        )

    def _log_stats(self) -> LogStats:
        logs_dir = self._root / "logs"
        active: list[LogFileStat] = []
        archived_count = 0
        archived_bytes = 0
        if logs_dir.is_dir():
            for path in sorted(logs_dir.glob("*")):
                if not path.is_file():
                    continue
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                if path.name.endswith(ARCHIVE_SUFFIX):
                    archived_count += 1
                    archived_bytes += size
                else:
                    active.append(LogFileStat(name=path.name, bytes=size))
        state = self._read_state()
        return LogStats(
            files=active,
            archived_count=archived_count,
            archived_bytes=archived_bytes,
            keep_days=LOG_ARCHIVE_KEEP_DAYS,
            last_rotate_at=state.get("last_rotate_at"),
        )

    # ---------- 轮转 ----------

    def rotate_logs(self, *, actor: str) -> LogRotateResult:
        with self._rotate_lock:
            logs_dir = self._root / "logs"
            rotated: list[LogRotateItem] = []
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            if logs_dir.is_dir():
                for source in sorted(p for p in logs_dir.glob("*.log") if p.is_file()):
                    archive_path = source.with_name(
                        f"{source.stem}-{stamp}{ARCHIVE_SUFFIX}"
                    )
                    counter = 1
                    while archive_path.exists():
                        archive_path = source.with_name(
                            f"{source.stem}-{stamp}-{counter}{ARCHIVE_SUFFIX}"
                        )
                        counter += 1
                    copied = self._gzip_copy(source, archive_path)
                    with source.open("r+b") as handle:
                        handle.truncate(0)
                    rotated.append(
                        LogRotateItem(
                            file=source.name,
                            archived_to=archive_path.name,
                            original_bytes=copied,
                        )
                    )
            purged_names, purged_bytes = self._purge_expired_archives(logs_dir)

            result = LogRotateResult(
                rotated=rotated,
                purged_archives=purged_names,
                purged_archives_bytes=purged_bytes,
                bytes_freed_total=sum(item.original_bytes for item in rotated)
                + purged_bytes,
                actor=actor,
            )
            self._write_state({"last_rotate_at": utcnow(), "actor": actor})
            freed = result.bytes_freed_total
            self._audit.record(
                RecordAuditEvent(
                    event_type="storage.logs.rotated",
                    level="info",
                    message=(
                        f"rotated={len(rotated)} purged={len(purged_names)} "
                        f"freed={freed} actor={actor}"
                    ),
                    payload=json.loads(result.model_dump_json()),
                )
            )
            return result

    def _auto_rotate_if_due(self) -> LogRotateResult | None:
        state = self._read_state()
        last = state.get("last_rotate_at")
        if last:
            try:
                parsed = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
            except ValueError:
                parsed = None
            if parsed is None:
                stale = False
            else:
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                stale = datetime.now(timezone.utc) - parsed >= AUTO_ROTATE_INTERVAL
            if not stale:
                return None
        try:
            return self.rotate_logs(actor=AUTO_ROTATE_ACTOR)
        except Exception:
            # 概览必须保持可用；失败不改 state，下次调用会重试。
            LOGGER.warning("automatic log rotation failed", exc_info=True)
            return None

    def _gzip_copy(self, source: Path, destination: Path) -> int:
        copied = 0
        try:
            with source.open("rb") as fin, gzip.open(destination, "wb") as fout:
                while chunk := fin.read(1024 * 1024):
                    copied += len(chunk)
                    fout.write(chunk)
            verified = 0
            with gzip.open(destination, "rb") as check:
                while chunk := check.read(1024 * 1024):
                    verified += len(chunk)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        if verified != copied:
            destination.unlink(missing_ok=True)
            raise RuntimeError(f"log archive verification failed for {source.name}")
        return copied

    def _purge_expired_archives(self, logs_dir: Path) -> tuple[list[str], int]:
        if not logs_dir.is_dir():
            return [], 0
        cutoff = time.time() - LOG_ARCHIVE_KEEP_DAYS * 86_400
        purged: list[str] = []
        purged_bytes = 0
        for archive in sorted(logs_dir.glob(f"*{ARCHIVE_SUFFIX}")):
            try:
                stat_result = archive.stat()
            except OSError:
                continue
            if stat_result.st_mtime < cutoff:
                purged.append(archive.name)
                purged_bytes += stat_result.st_size
                archive.unlink()
        return purged, purged_bytes

    # ---------- 状态文件 ----------

    def _state_path(self) -> Path:
        return self._root / "storage" / "state.json"

    def _read_state(self) -> dict:
        path = self._state_path()
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write_state(self, payload: dict) -> None:
        path = self._state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
