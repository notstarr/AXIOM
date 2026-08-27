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
SNAPSHOT_REFRESH_INTERVAL = timedelta(minutes=10)
ARCHIVE_SUFFIX = ".log.gz"
AUTO_ROTATE_ACTOR = "system.storage-auto"
STORAGE_DIR_NAME = "storage"
STATE_FILE = "state.json"
SNAPSHOT_FILE = "usage.json"

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


class StorageAdminService:
    """只读存储盘点 + 日志 copytruncate 轮转。

    耗时操作与请求路径严格分离：
    - 80G 级目录遍历只在后台重建线程里跑，产物持久化为 storage/usage.json 快照，
      GET /overview 恒定毫秒级返回缓存并在重建期间置 refreshing=true；
    - launchd 重定向的日志必须 copytruncate（复制 → gzip 校验 → 截断原文件），
      进程无需感知；轮转的自动触发在后台重建例程开头执行，绝不阻塞请求。
    """

    def __init__(self, engine: Engine, store: FileStore):
        self.engine = engine
        self._root = store.root
        self._audit = AuditService(engine)
        self._rotate_lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._refreshing = False

    # ---------- 请求路径（毫秒级） ----------

    def overview(self) -> StorageOverview:
        snapshot = self._read_snapshot()
        refreshing = False
        if snapshot is None:
            refreshing = True
            self.ensure_refresh_scheduled()
        elif self._snapshot_age(snapshot) >= SNAPSHOT_REFRESH_INTERVAL:
            refreshing = True
            self.ensure_refresh_scheduled()
        base = snapshot or self._empty_overview()
        base["refreshing"] = refreshing
        base["disk"] = self._disk_stats()
        base["database"] = self._database_stats()
        base["logs"] = self._log_stats().model_dump()
        return StorageOverview.model_validate(base)

    def refresh(self, *, actor: str = AUTO_ROTATE_ACTOR) -> StorageOverview:
        """同步重建：先执行到期轮转，再全量盘点并落快照。供手动强制与测试使用。"""
        if self._rotate_due():
            try:
                self.rotate_logs(actor=actor)
            except Exception:
                LOGGER.warning("lazy log rotation failed", exc_info=True)
        overview = self._compute_full_overview()
        self._write_snapshot(overview)
        return overview

    def ensure_refresh_scheduled(self) -> bool:
        """快照缺失或过期时用后台单飞线程重建；已在跑或刚跑过则返回 False。"""
        with self._refresh_lock:
            if self._refreshing:
                return False
            snapshot = self._read_snapshot()
            if snapshot is not None and (
                self._snapshot_age(snapshot) < SNAPSHOT_REFRESH_INTERVAL
            ):
                return False
            self._refreshing = True
        thread = threading.Thread(
            target=self._safe_background_refresh,
            name="axiom-storage-refresh",
            daemon=True,
        )
        thread.start()
        return True

    def _safe_background_refresh(self) -> None:
        try:
            self.refresh(actor=AUTO_ROTATE_ACTOR)
        except Exception:
            LOGGER.warning("background storage refresh failed", exc_info=True)
        finally:
            with self._refresh_lock:
                self._refreshing = False

    def _rotate_due(self) -> bool:
        state = self._read_state()
        last = state.get("last_rotate_at")
        if not last:
            return True
        try:
            parsed = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
        except ValueError:
            return True
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - parsed >= AUTO_ROTATE_INTERVAL

    def _compute_full_overview(self) -> StorageOverview:
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
            refreshing=False,
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

    # ---------- 快路径数据源 ----------

    def _disk_stats(self) -> DiskStats:
        disk_total, _, disk_free = shutil.disk_usage(self._root)
        return DiskStats(total_bytes=disk_total, free_bytes=disk_free)

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

    # ---------- 快照与状态文件 ----------

    def _snapshot_age(self, snapshot: dict) -> timedelta:
        generated = str(snapshot.get("generated_at") or "")
        try:
            parsed = datetime.fromisoformat(generated.replace("Z", "+00:00"))
        except ValueError:
            return SNAPSHOT_REFRESH_INTERVAL + timedelta(days=1)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - parsed

    def _read_snapshot(self) -> dict | None:
        path = self._root / STORAGE_DIR_NAME / SNAPSHOT_FILE
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        return payload

    def _write_snapshot(self, overview: StorageOverview) -> None:
        self._atomic_json(SNAPSHOT_FILE, overview.model_dump(mode="json"))

    def _empty_overview(self) -> dict:
        return {
            "generated_at": utcnow(),
            "data_dir": str(self._root),
            "total_bytes": 0,
            "categories": [],
            "refreshing": True,
        }

    def _read_state(self) -> dict:
        try:
            return json.loads((self._root / STORAGE_DIR_NAME / STATE_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write_state(self, payload: dict) -> None:
        self._atomic_json(STATE_FILE, payload)

    def _atomic_json(self, name: str, payload: dict) -> None:
        directory = self._root / STORAGE_DIR_NAME
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / name
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, target)
