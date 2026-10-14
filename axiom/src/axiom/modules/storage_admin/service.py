from __future__ import annotations

import gzip
import json
import logging
import os
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import Engine, text

from axiom.modules.audit.schemas import RecordAuditEvent
from axiom.modules.audit.service import AuditService
from axiom.modules.context.retention import (
    ContextRetentionService,
    RetentionPlan,
    RetentionRunResult,
)
from axiom.shared.clock import utcnow
from axiom.shared.errors import ConflictError, DomainError
from axiom.shared.storage import FileStore

from .schemas import (
    CleanupExecuteResult,
    CleanupItem,
    CleanupPlan,
    CleanupPreviewItem,
    CleanupPreviewTarget,
    CleanupRemovedItem,
    CleanupSkippedItem,
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

# ── 清理中心 ──
CLEANUP_PLAN_FILE = "cleanup-plan.json"
CLEANUP_PLAN_TTL = timedelta(minutes=30)
LEASE_HOT_WINDOW = timedelta(minutes=30)
SQLITE_BACKUP_KEEP_DAYS = 30
PG_BACKUP_KEEP_COUNT = 3
ACTIVE_LEASE_STATUSES = ("claimed", "running", "concluding")
CLEANUP_ALLOWED_PREFIXES = (
    "runs/",
    "sandboxes/",
    "contexts/",
    "tasks/",
    "backups/",
    "maintenance/",
    "postgresql-backups/",
    "logs/",
    "storage/usage.json",
)
LEASE_DIR_PREFIX = "lease_"

# 计入分类盘点的业务目录。`.staging` 等内部临时目录不单列，
# 它们的体积已包含在 total_bytes 与各分类差值中。
CATEGORY_DIRS = (
    "runs",
    "sandboxes",
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
        self._cleanup_refresh_lock = threading.Lock()
        self._cleanup_refreshing = False
        self.context_retention = ContextRetentionService(engine, store)
        self._context_retention_thread: threading.Thread | None = None
        self._context_retention_stop = threading.Event()

    def start_context_retention_loop(self) -> bool:
        """Start the optional hourly GC without putting work on request paths."""
        if not self.context_retention.config.enabled:
            return False
        if self._context_retention_thread is not None and self._context_retention_thread.is_alive():
            return False
        try:
            interval = max(60, int(os.getenv("AXIOM_CONTEXT_GC_INTERVAL_SECONDS", "3600")))
        except ValueError:
            interval = 3600

        def loop() -> None:
            while not self._context_retention_stop.wait(interval):
                try:
                    self.context_retention_run(dry_run=False, actor="system.context-gc")
                except Exception:
                    LOGGER.warning("background Context retention failed", exc_info=True)

        self._context_retention_thread = threading.Thread(
            target=loop,
            name="axiom-context-retention",
            daemon=True,
        )
        self._context_retention_thread.start()
        return True

    def stop_context_retention_loop(self) -> None:
        self._context_retention_stop.set()

    def context_retention_plan(self) -> RetentionPlan:
        return self.context_retention.plan()

    def context_retention_run(
        self,
        *,
        dry_run: bool,
        actor: str,
        max_revisions: int | None = None,
        max_bytes: int | None = None,
    ) -> RetentionRunResult:
        result = self.context_retention.run(
            dry_run=dry_run,
            actor=actor,
            max_revisions=max_revisions,
            max_bytes=max_bytes,
        )
        self._audit.record(
            RecordAuditEvent(
                event_type="storage.context_retention.executed",
                level="info",
                message=(
                    f"dry_run={dry_run} removed={len(result.removed)} "
                    f"freed={result.freed_bytes} actor={actor}"
                ),
                payload={
                    "dry_run": dry_run,
                    "paused": result.paused,
                    "candidate_paths": [
                        *[item.path for item in result.removed],
                        *[item["path"] for item in result.skipped if item.get("path")],
                    ],
                    "removed": [item.path for item in result.removed],
                    "freed_bytes": result.freed_bytes,
                    "skipped": result.skipped,
                },
            )
        )
        return result

    def pause_context_retention(self) -> None:
        self.context_retention.pause()

    def resume_context_retention(self) -> None:
        self.context_retention.resume()

    # ---------- 请求路径（毫秒级） ----------

    def overview(self) -> StorageOverview:
        snapshot = self._read_snapshot()
        stale = snapshot is None or self._snapshot_age(snapshot) >= SNAPSHOT_REFRESH_INTERVAL
        if stale:
            self.ensure_refresh_scheduled()
        # 线程在跑或快照缺失/过期都视为 refreshing，前端据此持续轮询。
        refreshing = stale or self._refreshing
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
        """全量盘点。contexts/ 含数百万小文件，逐文件 stat 需 4 分钟级——
        该分类优先复用清理计划快照（bytes+items）；快照尚未迁移到新格式时
        沿用上一次概览数值，绝不现场全量遍历（迁移由清理计划后台重算完成）。"""
        snapshot = self._cleanup_snapshot()
        context_sizes = snapshot.get("context_sizes") or {}
        contexts_cached = (
            bool(context_sizes)
            and all(isinstance(v, dict) and v.get("items") is not None for v in context_sizes.values())
        )
        prev_categories = {
            item.get("key"): item
            for item in (self._read_snapshot() or {}).get("categories", [])
        }

        categories: list[StorageCategory] = []
        total_bytes = 0
        for rel in CATEGORY_DIRS:
            path = self._root / rel
            if not path.is_dir():
                continue
            if rel == "contexts" and contexts_cached:
                size_bytes = sum(int(v["bytes"]) for v in context_sizes.values())
                items = sum(int(v.get("items", 0)) for v in context_sizes.values())
            elif rel == "contexts" and "contexts" in prev_categories:
                prev = prev_categories["contexts"]
                size_bytes = int(prev.get("bytes", 0))
                items = int(prev.get("items", 0))
            else:
                size_bytes, items = _tree_stats(path)
            total_bytes += size_bytes
            categories.append(
                StorageCategory(key=rel, path=str(path), bytes=size_bytes, items=items)
            )
        # 根目录剩余部分：散文件 + 未归类目录（contexts 已由缓存覆盖，跳过）
        for entry in self._root.iterdir():
            if entry.name in CATEGORY_DIRS:
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    dir_bytes, _items = _tree_stats(entry)
                    total_bytes += dir_bytes
                elif entry.is_file(follow_symlinks=False):
                    total_bytes += entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
        categories.sort(key=lambda item: item.bytes, reverse=True)

        disk_total, _, disk_free = shutil.disk_usage(self._root)
        return StorageOverview(
            generated_at=utcnow(),
            data_dir=str(self._root),
            total_bytes=total_bytes,
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

    # ══════════════ 清理中心（三档） ══════════════
    #
    # 一档：可再生/纯流水（lease process 日志、超期日志归档、usage 快照），默认全选一键清。
    # 二档：人工预览勾选后删除（已完结任务的 run 产物 / contexts / 已归档任务 artifacts /
    #       超期 sqlite 备份 / maintenance 快照 / 第 4 份及更旧的 PG 备份）。
    # 三档：锁定展示（活跃任务、迁移证据、运行时依赖、最新 PG 备份）。
    #
    # 安全护栏：preview 与 execute 共用 _resolve_cleanup_targets 单一事实源，
    # execute 时重新解析（重新查活跃租约、重新检查热文件 mtime），路径经
    # _validated_delete 白名单校验；全部动作写审计。

    def cleanup_plan(self) -> CleanupPlan:
        snapshot = self._read_json_file(Path(STORAGE_DIR_NAME) / CLEANUP_PLAN_FILE)
        stale = snapshot is None or self._json_age(snapshot.get("generated_at")) >= CLEANUP_PLAN_TTL
        if stale:
            self.ensure_cleanup_plan_scheduled()
        if snapshot is None:
            snapshot = {"generated_at": utcnow(), "tier1": [], "tier2": [], "tier3": []}
        return CleanupPlan(
            generated_at=snapshot.get("generated_at") or utcnow(),
            data_dir=str(self._root),
            refreshing=stale or self._cleanup_refreshing,
            tier1=[CleanupItem(**item) for item in snapshot.get("tier1", [])],
            tier2=[CleanupItem(**item) for item in snapshot.get("tier2", [])],
            tier3=[CleanupItem(**item) for item in snapshot.get("tier3", [])],
        )

    def ensure_cleanup_plan_scheduled(self) -> bool:
        with self._cleanup_refresh_lock:
            if self._cleanup_refreshing:
                return False
            snapshot = self._read_json_file(Path(STORAGE_DIR_NAME) / CLEANUP_PLAN_FILE)
            if snapshot is not None and self._json_age(snapshot.get("generated_at")) < CLEANUP_PLAN_TTL:
                return False
            self._cleanup_refreshing = True
        thread = threading.Thread(
            target=self._safe_cleanup_refresh,
            name="axiom-cleanup-plan",
            daemon=True,
        )
        thread.start()
        return True

    def _safe_cleanup_refresh(self) -> None:
        try:
            plan, index = self._compute_cleanup_plan()
            self._write_cleanup_snapshot(plan, index)
        except Exception:
            LOGGER.warning("background cleanup plan failed", exc_info=True)
        finally:
            with self._cleanup_refresh_lock:
                self._cleanup_refreshing = False

    def refresh_cleanup_plan(self) -> CleanupPlan:
        plan, _index = self._compute_cleanup_plan()
        self._write_cleanup_snapshot(plan, _index)
        return plan

    def _write_cleanup_snapshot(self, plan: CleanupPlan, index: dict) -> None:
        payload = plan.model_dump(mode="json")
        payload.update(index)
        self._atomic_json(CLEANUP_PLAN_FILE, payload)

    def _cleanup_snapshot(self) -> dict:
        return self._read_json_file(Path(STORAGE_DIR_NAME) / CLEANUP_PLAN_FILE) or {}

    def _cleanup_context(self) -> dict:
        """preview/execute 共用的批量上下文：DB 状态只查一次，尺寸索引来自计划快照。"""
        snapshot = self._cleanup_snapshot()
        return {
            "task_states": self._task_states(),
            "active_leases": self._active_lease_ids(),
            "lease_index": snapshot.get("lease_index") or {},
            "context_sizes": snapshot.get("context_sizes") or {},
            "artifact_sizes": snapshot.get("artifact_sizes") or {},
        }

    # ---------- 数据采集 ----------

    def _active_lease_ids(self) -> set[str]:
        status_list = ", ".join(f"'{s}'" for s in ACTIVE_LEASE_STATUSES)
        query = " UNION ".join(
            f"SELECT lease_id FROM axiom.{table} WHERE lease_id IS NOT NULL AND status IN ({status_list})"
            for table in ("intents", "reason_runs", "plan_runs", "vulnerability_review_runs")
        )
        try:
            with self.engine.connect() as conn:
                rows = conn.execute(text(query)).scalars().all()
        except Exception:
            LOGGER.warning("active lease query failed; treating all leases as protected", exc_info=True)
            return set()
        return {str(row) for row in rows if row}

    def _task_states(self) -> dict[str, dict]:
        try:
            with self.engine.connect() as conn:
                rows = conn.execute(
                    text("SELECT id, status, archived FROM axiom.tasks")
                ).mappings().all()
        except Exception:
            LOGGER.warning("task state query failed; treating all tasks as protected", exc_info=True)
            return {}
        return {
            str(row["id"]): {"status": str(row["status"]), "archived": bool(row["archived"])}
            for row in rows
        }

    def _lease_stats(self, lease_dir: Path) -> tuple[int, int, float]:
        """返回 (租约总体积, process 子目录体积, process 最新 mtime)。"""
        total = 0
        process_bytes = 0
        process_mtime = 0.0
        stack: list[tuple[str, bool]] = [(str(lease_dir), False)]
        while stack:
            current, in_process = stack.pop()
            try:
                entries = list(os.scandir(current))
            except OSError:
                continue
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append((entry.path, in_process or entry.name == "process"))
                    elif entry.is_file(follow_symlinks=False):
                        stat = entry.stat(follow_symlinks=False)
                        total += stat.st_size
                        if in_process:
                            process_bytes += stat.st_size
                            process_mtime = max(process_mtime, stat.st_mtime)
                except OSError:
                    continue
        return total, process_bytes, process_mtime

    def _is_lease_hot(self, process_mtime: float) -> bool:
        if process_mtime <= 0:
            return False
        return time.time() - process_mtime < LEASE_HOT_WINDOW.total_seconds()

    def _lease_process_mtime(self, process_dir: Path) -> float:
        """process 活跃度：目录与 stdout/stderr 文件 mtime 取最新。

        只看目录 mtime 不够——持续写入 stdout.log 不会更新父目录 mtime，
        会把活跃租约误判为可清理。
        """
        mtimes: list[float] = []
        try:
            mtimes.append(process_dir.stat().st_mtime)
        except OSError:
            return 0.0
        for name in ("stdout.log", "stderr.log"):
            try:
                mtimes.append((process_dir / name).stat().st_mtime)
            except OSError:
                continue
        return max(mtimes) if mtimes else 0.0

    def _iter_task_leases(self, task_id: str | None = None):
        """遍历 runs/ 下的租约目录，产出 (task_id, unit_id, lease_id, lease_dir)。"""
        runs_root = self._root / "runs"
        if not runs_root.is_dir():
            return
        try:
            task_entries = sorted(os.scandir(runs_root), key=lambda e: e.name)
        except OSError:
            return
        for task_entry in task_entries:
            if task_id and task_entry.name != task_id:
                continue
            if not task_entry.is_dir(follow_symlinks=False):
                continue
            try:
                unit_entries = sorted(os.scandir(task_entry.path), key=lambda e: e.name)
            except OSError:
                continue
            for unit_entry in unit_entries:
                if not unit_entry.is_dir(follow_symlinks=False):
                    continue
                try:
                    lease_entries = sorted(os.scandir(unit_entry.path), key=lambda e: e.name)
                except OSError:
                    continue
                for lease_entry in lease_entries:
                    if not lease_entry.name.startswith(LEASE_DIR_PREFIX):
                        continue
                    if not lease_entry.is_dir(follow_symlinks=False):
                        continue
                    yield (
                        task_entry.name,
                        unit_entry.name,
                        lease_entry.name[len(LEASE_DIR_PREFIX):],
                        Path(lease_entry.path),
                    )

    def _dir_size(self, path: Path) -> int:
        if not path.exists():
            return 0
        return _tree_stats(path)[0]

    def _entry_mtime(self, path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    def _expired_sqlite_backups(self) -> list[Path]:
        cutoff = time.time() - SQLITE_BACKUP_KEEP_DAYS * 86_400
        found: list[Path] = []
        backups_dir = self._root / "backups"
        if backups_dir.is_dir():
            for entry in backups_dir.iterdir():
                if entry.is_symlink():
                    continue
                if self._entry_mtime(entry) < cutoff:
                    found.append(entry)
        for entry in self._root.glob("axiom.db.*"):
            if entry.is_file() and not entry.is_symlink() and self._entry_mtime(entry) < cutoff:
                found.append(entry)
        return sorted(found, key=lambda p: p.name)

    def _pg_backup_buckets(self) -> tuple[list[Path], list[Path]]:
        """返回 (保留的最新 N 份, 可清理的更旧份)。"""
        backups_dir = self._root / "postgresql-backups"
        if not backups_dir.is_dir():
            return [], []
        files = [
            entry for entry in backups_dir.iterdir()
            if entry.is_file() and not entry.is_symlink()
        ]
        files.sort(key=lambda p: self._entry_mtime(p), reverse=True)
        return files[:PG_BACKUP_KEEP_COUNT], files[PG_BACKUP_KEEP_COUNT:]

    def _expired_log_archives(self) -> tuple[list[Path], int]:
        cutoff = time.time() - LOG_ARCHIVE_KEEP_DAYS * 86_400
        logs_dir = self._root / "logs"
        found: list[Path] = []
        total = 0
        if logs_dir.is_dir():
            for archive in sorted(logs_dir.glob(f"*{ARCHIVE_SUFFIX}")):
                try:
                    stat = archive.stat()
                except OSError:
                    continue
                if stat.st_mtime < cutoff:
                    found.append(archive)
                    total += stat.st_size
        return found, total

    # ---------- 目标解析（preview / execute 共用单一事实源） ----------

    def _resolve_cleanup_targets(
        self, key: str, ctx: dict | None = None,
    ) -> tuple[list[CleanupPreviewTarget], int, str]:
        """解析一个清理 key 的具体删除目标。返回 (targets, protected_count, warning)。

        ctx 为批量上下文（_cleanup_context 产物）：DB 状态只查一次，尺寸索引来自
        计划快照（lease_index/context_sizes/artifact_sizes），避免逐 key 全量重扫。
        索引缺失时回退到慢路径（现场扫描）。
        """
        context = ctx or self._cleanup_context()
        task_states = context["task_states"]
        active_leases = context["active_leases"]
        lease_index = context["lease_index"]

        if key == "t1_lease_process":
            targets: list[CleanupPreviewTarget] = []
            protected = 0
            cached_leases = [
                entry
                for leases in lease_index.values()
                for entry in leases
            ]
            if cached_leases:
                for entry in cached_leases:
                    rel_lease = entry["path"]
                    lease_dir = self._root / rel_lease
                    process_dir = lease_dir / "process"
                    if not process_dir.is_dir():
                        continue
                    try:
                        mtime = self._lease_process_mtime(process_dir)
                    except OSError:
                        continue
                    lease_id = Path(rel_lease).name[len(LEASE_DIR_PREFIX):]
                    if lease_id in active_leases or self._is_lease_hot(mtime):
                        protected += 1
                        continue
                    if entry.get("process", 0) <= 0:
                        continue
                    targets.append(CleanupPreviewTarget(
                        path=f"{rel_lease}/process", bytes=entry["process"], kind="lease_process",
                    ))
            else:
                for _task_id, _unit, lease_id, lease_dir in self._iter_task_leases():
                    _total, process_bytes, process_mtime = self._lease_stats(lease_dir)
                    if lease_id in active_leases or self._is_lease_hot(process_mtime):
                        protected += 1
                        continue
                    if process_bytes <= 0:
                        continue
                    targets.append(CleanupPreviewTarget(
                        path=(lease_dir / "process").relative_to(self._root).as_posix(),
                        bytes=process_bytes, kind="lease_process",
                    ))
            return targets, protected, ""

        if key == "t1_log_archives":
            expired, _total = self._expired_log_archives()
            targets = [
                CleanupPreviewTarget(
                    path=archive.relative_to(self._root).as_posix(),
                    bytes=archive.stat().st_size, kind="log_archive",
                )
                for archive in expired
            ]
            return targets, 0, ""

        if key == "t1_usage_snapshot":
            path = self._root / STORAGE_DIR_NAME / SNAPSHOT_FILE
            if path.is_file():
                return [CleanupPreviewTarget(
                    path=path.relative_to(self._root).as_posix(),
                    bytes=path.stat().st_size, kind="snapshot",
                )], 0, ""
            return [], 0, ""

        if key.startswith("t2_task_runs:"):
            task_id = key.split(":", 1)[1]
            # DB 无记录 = 任务已被清除的孤儿目录，无任何引用，按已完结处理。
            state = task_states.get(task_id) or {"status": "stopped", "archived": True}
            if state["status"] == "running":
                raise ConflictError(f"task {task_id} is running; cleanup is locked")
            targets = []
            protected = 0
            task_dir = self._root / "runs" / task_id
            cached_entries = lease_index.get(task_id) or []
            if cached_entries:
                for entry in cached_entries:
                    rel_lease = entry["path"]
                    lease_dir = self._root / rel_lease
                    if not lease_dir.is_dir():
                        continue
                    process_dir = lease_dir / "process"
                    try:
                        mtime = self._lease_process_mtime(process_dir) if process_dir.is_dir() else 0.0
                    except OSError:
                        mtime = 0.0
                    lease_id = Path(rel_lease).name[len(LEASE_DIR_PREFIX):]
                    if lease_id in active_leases or self._is_lease_hot(mtime):
                        protected += 1
                        continue
                    targets.append(CleanupPreviewTarget(
                        path=rel_lease, bytes=entry.get("total", 0), kind="lease",
                    ))
            else:
                for _t, _u, lease_id, lease_dir in self._iter_task_leases(task_id):
                    total, _process_bytes, process_mtime = self._lease_stats(lease_dir)
                    if lease_id in active_leases or self._is_lease_hot(process_mtime):
                        protected += 1
                        continue
                    targets.append(CleanupPreviewTarget(
                        path=lease_dir.relative_to(self._root).as_posix(),
                        bytes=total, kind="lease",
                    ))
            if task_dir.is_dir():
                for entry in task_dir.iterdir():
                    if entry.is_file() and not entry.is_symlink():
                        targets.append(CleanupPreviewTarget(
                            path=entry.relative_to(self._root).as_posix(),
                            bytes=entry.stat().st_size, kind="file",
                        ))
            return targets, protected, ""

        if key.startswith("t2_task_contexts:"):
            task_id = key.split(":", 1)[1]
            state = task_states.get(task_id) or {"status": "stopped", "archived": True}
            if state["status"] == "running" or self._task_has_active_lease(task_id):
                raise ConflictError(f"task {task_id} has an active Worker lease; cleanup is locked")
            if not state["archived"]:
                raise ConflictError(
                    f"task {task_id} is not archived; use revision-level Context retention"
                )
            path = self._root / "contexts" / task_id
            if not path.is_dir():
                return [], 0, ""
            cached = context["context_sizes"].get(task_id)
            if isinstance(cached, dict):
                size = int(cached["bytes"])
            elif cached is not None:
                size = int(cached)
            else:
                size = _tree_stats(path)[0]
            return [CleanupPreviewTarget(
                path=path.relative_to(self._root).as_posix(),
                bytes=size, kind="dir",
            )], 0, ""

        if key.startswith("t2_task_artifacts:"):
            task_id = key.split(":", 1)[1]
            state = task_states.get(task_id) or {"status": "stopped", "archived": True}
            if state["status"] == "running":
                raise ConflictError(f"task {task_id} is running; cleanup is locked")
            if not state["archived"]:
                raise ConflictError(
                    f"task {task_id} is not archived; artifacts cleanup requires archive first"
                )
            task_dir = self._root / "tasks" / task_id
            if not task_dir.is_dir():
                return [], 0, ""
            targets = []
            for current, _dirs, files in os.walk(task_dir):
                for name in files:
                    file_path = Path(current) / name
                    try:
                        if file_path.is_symlink():
                            continue
                        size = file_path.stat().st_size
                    except OSError:
                        continue
                    targets.append(CleanupPreviewTarget(
                        path=file_path.relative_to(self._root).as_posix(),
                        bytes=size, kind="artifact",
                    ))
            return targets, 0, "删除后文件中心对应产物将无法下载（数据库记录保留）。"

        if key == "t2_sqlite_backups":
            targets = [
                CleanupPreviewTarget(
                    path=entry.relative_to(self._root).as_posix(),
                    bytes=_tree_stats(entry)[0] if entry.is_dir() else entry.stat().st_size,
                    kind="sqlite_backup",
                )
                for entry in self._expired_sqlite_backups()
            ]
            return targets, 0, ""

        if key.startswith("t2_maintenance:"):
            name = key.split(":", 1)[1]
            if not name or "/" in name or "\\" in name or name.startswith("."):
                raise DomainError(f"invalid maintenance entry: {name}")
            entry = self._root / "maintenance" / name
            if not entry.exists() or entry.is_symlink():
                raise DomainError(f"unknown maintenance entry: {name}")
            size = _tree_stats(entry)[0] if entry.is_dir() else entry.stat().st_size
            return [CleanupPreviewTarget(
                path=entry.relative_to(self._root).as_posix(), bytes=size, kind="maintenance",
            )], 0, ""

        if key == "t2_pg_backups":
            _kept, purgeable = self._pg_backup_buckets()
            return [
                CleanupPreviewTarget(
                    path=entry.relative_to(self._root).as_posix(),
                    bytes=entry.stat().st_size, kind="pg_backup",
                )
                for entry in purgeable
            ], 0, ""

        raise DomainError(f"unknown cleanup key: {key}")

    def _task_has_active_lease(self, task_id: str) -> bool:
        statuses = ", ".join(f"'{status}'" for status in ACTIVE_LEASE_STATUSES)
        query = " UNION ".join(
            f"SELECT 1 FROM axiom.{table} WHERE task_id = :task_id "
            f"AND lease_id IS NOT NULL AND status IN ({statuses})"
            for table in ("intents", "reason_runs", "plan_runs", "vulnerability_review_runs")
        )
        with self.engine.connect() as conn:
            return conn.execute(text(f"SELECT EXISTS ({query})"), {"task_id": task_id}).scalar_one()

    # ---------- API 入口 ----------

    def cleanup_preview(self, keys: list[str]) -> list[CleanupPreviewItem]:
        titles = self._cleanup_titles()
        context = self._cleanup_context()
        items: list[CleanupPreviewItem] = []
        for key in keys:
            targets, protected, warning = self._resolve_cleanup_targets(key, context)
            items.append(CleanupPreviewItem(
                key=key,
                title=titles.get(key, key),
                targets=targets,
                total_bytes=sum(target.bytes for target in targets),
                protected_count=protected,
                warning=warning,
            ))
        return items

    def cleanup_execute(self, keys: list[str], excluded: list[str], actor: str) -> CleanupExecuteResult:
        excluded_set = {item.strip() for item in excluded if item and item.strip()}
        context = self._cleanup_context()
        removed: list[dict] = []
        skipped: list[dict] = []
        freed_total = 0
        for key in keys:
            try:
                targets, _protected, _warning = self._resolve_cleanup_targets(key, context)
            except DomainError as exc:
                skipped.append({"key": key, "path": "", "reason": str(exc)})
                continue
            for target in targets:
                if target.path in excluded_set:
                    skipped.append({"key": key, "path": target.path, "reason": "excluded by operator"})
                    continue
                try:
                    freed = self._validated_delete(target.path)
                except DomainError as exc:
                    skipped.append({"key": key, "path": target.path, "reason": str(exc)})
                    continue
                except FileNotFoundError:
                    skipped.append({"key": key, "path": target.path, "reason": "already gone"})
                    continue
                freed_total += freed
                removed.append({"key": key, "path": target.path, "bytes": freed})
        result = CleanupExecuteResult(
            freed_bytes=freed_total,
            removed=[CleanupRemovedItem(**item) for item in removed],
            skipped=[CleanupSkippedItem(**item) for item in skipped],
            actor=actor,
        )
        self._audit.record(
            RecordAuditEvent(
                event_type="storage.cleanup.executed",
                level="info",
                message=(
                    f"keys={len(keys)} removed={len(removed)} skipped={len(skipped)} "
                    f"freed={freed_total} actor={actor}"
                ),
                payload=json.loads(result.model_dump_json()),
            )
        )
        # 清理改变了体积分布：作废两份缓存，下次读取时后台重建。
        for name in (SNAPSHOT_FILE, CLEANUP_PLAN_FILE):
            try:
                (self._root / STORAGE_DIR_NAME / name).unlink(missing_ok=True)
            except OSError:
                pass
        return result

    def _validated_delete(self, rel_path: str) -> int:
        root = self._root.resolve()
        target = (self._root / rel_path).resolve()
        if target == root or root not in target.parents:
            raise DomainError(f"path outside data dir: {rel_path}")
        if not any(rel_path == prefix.rstrip("/") or rel_path.startswith(prefix) for prefix in CLEANUP_ALLOWED_PREFIXES):
            raise DomainError(f"path not allowed for cleanup: {rel_path}")
        if not target.exists():
            raise DomainError(f"path not found: {rel_path}")
        # PG 备份底线：无论来源，最新 N 份永不删除。
        if rel_path.startswith("postgresql-backups/"):
            kept, _purgeable = self._pg_backup_buckets()
            kept_names = {entry.name for entry in kept}
            if Path(rel_path).name in kept_names:
                raise ConflictError(f"pg backup is within keep window: {rel_path}")
        size = _tree_stats(target)[0] if target.is_dir() else target.stat().st_size
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        return size

    # ---------- 计划构建 ----------

    def _cleanup_titles(self) -> dict[str, str]:
        plan = self._read_json_file(Path(STORAGE_DIR_NAME) / CLEANUP_PLAN_FILE) or {}
        titles: dict[str, str] = {}
        for tier in ("tier1", "tier2", "tier3"):
            for item in plan.get(tier, []):
                titles[item.get("key", "")] = item.get("title", "")
        return titles

    def _compute_cleanup_plan(self) -> tuple[CleanupPlan, dict]:
        task_states = self._task_states()
        active_leases = self._active_lease_ids()
        # 增量复用：contexts/ 有数百万小文件，全量 stat 需要 4 分钟级。
        # 上一份快照里已有的任务尺寸直接沿用（已完结任务不可变；活跃任务
        # 仅用于三档展示，允许滞后），只对新增任务现场统计。
        previous = self._cleanup_snapshot()
        prev_context_sizes = previous.get("context_sizes") or {}
        prev_artifact_sizes = previous.get("artifact_sizes") or {}
        prev_leases = previous.get("lease_index") or {}

        # ── runs/ 按任务聚合（同时记录逐租约索引，供 preview/execute 免重扫） ──
        run_tasks: dict[str, dict] = {}
        lease_index: dict[str, list[dict]] = {}
        t1_process_bytes = 0
        t1_process_count = 0
        for task_id, _unit, lease_id, lease_dir in self._iter_task_leases():
            process_dir = lease_dir / "process"
            try:
                fresh_mtime = self._lease_process_mtime(process_dir) if process_dir.is_dir() else 0.0
            except OSError:
                fresh_mtime = 0.0
            cached_entry = next(
                (item for item in prev_leases.get(task_id, []) if item["path"] == lease_dir.relative_to(self._root).as_posix()),
                None,
            )
            is_protected = lease_id in active_leases or self._is_lease_hot(fresh_mtime)
            if (
                cached_entry is not None
                and abs(cached_entry.get("mtime", -1) - fresh_mtime) < 1
                and not is_protected
            ):
                # process 目录 mtime 未变 → 尺寸沿用缓存，跳过全量遍历
                total = cached_entry.get("total", 0)
                process_bytes = cached_entry.get("process", 0)
            else:
                total, process_bytes, _ = self._lease_stats(lease_dir)
            bucket = run_tasks.setdefault(task_id, {
                "total": 0, "protected": 0, "clean_lease_count": 0, "clean_lease_bytes": 0,
            })
            bucket["total"] += total
            lease_index.setdefault(task_id, []).append({
                "path": lease_dir.relative_to(self._root).as_posix(),
                "total": total,
                "process": process_bytes,
                "mtime": fresh_mtime,
                "protected": is_protected,
            })
            if is_protected:
                bucket["protected"] += 1
                continue
            bucket["clean_lease_count"] += 1
            bucket["clean_lease_bytes"] += total
            if process_bytes > 0:
                t1_process_bytes += process_bytes
                t1_process_count += 1

        # ── contexts/ 与 tasks/ 按任务（优先沿用快照，只统计新增） ──
        # contexts 值为 {"bytes", "items"}（概览分类需要文件数）；旧格式 int 视为
        # 缺失，迁移时现场统计一次。
        context_tasks: dict[str, dict] = {}
        contexts_root = self._root / "contexts"
        if contexts_root.is_dir():
            for entry in contexts_root.iterdir():
                if entry.is_dir() and not entry.is_symlink():
                    prev = prev_context_sizes.get(entry.name)
                    if isinstance(prev, dict) and prev.get("items") is not None:
                        context_tasks[entry.name] = prev
                    else:
                        size_bytes, items = _tree_stats(entry)
                        context_tasks[entry.name] = {"bytes": size_bytes, "items": items}
        artifact_tasks: dict[str, int] = {}
        tasks_root = self._root / "tasks"
        if tasks_root.is_dir():
            for entry in tasks_root.iterdir():
                if entry.is_dir() and not entry.is_symlink():
                    if entry.name in prev_artifact_sizes:
                        artifact_tasks[entry.name] = prev_artifact_sizes[entry.name]
                    else:
                        artifact_tasks[entry.name] = _tree_stats(entry)[0]

        # ── 一档 ──
        tier1: list[CleanupItem] = [
            CleanupItem(
                key="t1_lease_process",
                title="租约执行流水日志",
                description="已完结租约 process/ 下的 stdout/stderr RPC 全量流水，删除不影响任何产物文件与系统运行。",
                bytes=t1_process_bytes, count=t1_process_count, default_selected=True,
            ),
        ]
        expired_logs, expired_log_bytes = self._expired_log_archives()
        tier1.append(CleanupItem(
            key="t1_log_archives",
            title="超期日志归档",
            description=f"logs/ 下超过 {LOG_ARCHIVE_KEEP_DAYS} 天的压缩归档（到期后本会自动清理）。",
            bytes=expired_log_bytes, count=len(expired_logs), default_selected=True,
        ))
        usage_path = self._root / STORAGE_DIR_NAME / SNAPSHOT_FILE
        if usage_path.is_file():
            tier1.append(CleanupItem(
                key="t1_usage_snapshot",
                title="存储统计快照",
                description="storage/usage.json 可随时重建，删除后打开本页会自动重新统计。",
                bytes=usage_path.stat().st_size, count=1, default_selected=True,
            ))

        # ── 二档 ──
        tier2: list[CleanupItem] = []
        for task_id in sorted(set(run_tasks) | set(context_tasks)):
            state = task_states.get(task_id) or {"status": "stopped", "archived": True}
            if state["status"] == "running":
                continue
            bucket = run_tasks.get(task_id)
            if bucket and bucket["clean_lease_count"] > 0:
                tier2.append(CleanupItem(
                    key=f"t2_task_runs:{task_id}",
                    title=f"任务 {task_id} 执行产物",
                    description="runs/ 下已完结租约的产物文件（findings、资产清单等）与会话记录。",
                    bytes=bucket["clean_lease_bytes"], count=bucket["clean_lease_count"],
                ))
            if state["archived"] and context_tasks.get(task_id, {}).get('bytes', 0) > 0:
                tier2.append(CleanupItem(
                    key=f"t2_task_contexts:{task_id}",
                    title=f"任务 {task_id} 上下文快照",
                    description="contexts/ 下该任务的会话上下文快照，删除后无法回放该任务历史上下文。",
                    bytes=context_tasks[task_id].get('bytes', 0), count=1,
                ))
        for task_id in sorted(artifact_tasks):
            state = task_states.get(task_id) or {"status": "stopped", "archived": True}
            if state["status"] == "running" or not state["archived"]:
                continue
            if artifact_tasks[task_id] <= 0:
                continue
            tier2.append(CleanupItem(
                key=f"t2_task_artifacts:{task_id}",
                title=f"任务 {task_id} 产物文件（已归档）",
                description="tasks/ 下该归档任务的 artifacts 文件本体。",
                bytes=artifact_tasks[task_id], count=1,
                warning="删除后文件中心对应产物将无法下载（数据库记录保留）。",
            ))
        expired_backups = self._expired_sqlite_backups()
        if expired_backups:
            tier2.append(CleanupItem(
                key="t2_sqlite_backups",
                title="超期 SQLite 备份",
                description=f"backups/ 与根目录下超过 {SQLITE_BACKUP_KEEP_DAYS} 天的旧库备份。",
                bytes=sum(
                    (_tree_stats(p)[0] if p.is_dir() else p.stat().st_size) for p in expired_backups
                ),
                count=len(expired_backups),
            ))
        maintenance_root = self._root / "maintenance"
        if maintenance_root.is_dir():
            for entry in sorted(maintenance_root.iterdir(), key=lambda p: p.name):
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    size = _tree_stats(entry)[0]
                elif entry.is_file():
                    size = entry.stat().st_size
                else:
                    size = 0
                if size <= 0:
                    continue
                tier2.append(CleanupItem(
                    key=f"t2_maintenance:{entry.name}",
                    title=f"维护快照 {entry.name}",
                    description="maintenance/ 下的人工维护快照，确认不再需要后可删。",
                    bytes=size, count=1,
                ))
        _kept, purgeable_pg = self._pg_backup_buckets()
        if purgeable_pg:
            tier2.append(CleanupItem(
                key="t2_pg_backups",
                title="旧 PostgreSQL 备份",
                description=f"postgresql-backups/ 中超出最新 {PG_BACKUP_KEEP_COUNT} 份的更旧备份。",
                bytes=sum(entry.stat().st_size for entry in purgeable_pg),
                count=len(purgeable_pg),
            ))

        # ── 三档 ──
        tier3: list[CleanupItem] = []
        for task_id, state in sorted(task_states.items()):
            if state["status"] != "running":
                continue
            total = (
                run_tasks.get(task_id, {}).get("total", 0)
                + context_tasks.get(task_id, {}).get('bytes', 0)
                + artifact_tasks.get(task_id, 0)
            )
            tier3.append(CleanupItem(
                key=f"t3_task_active:{task_id}",
                title=f"活跃任务 {task_id}",
                description="任务正在运行，Worker 正在读写其 runs/contexts/artifacts，禁止清理。",
                bytes=total, count=1,
            ))
        kept_pg, _ = self._pg_backup_buckets()
        tier3.extend([
            CleanupItem(
                key="t3_pg_backups_recent",
                title=f"PostgreSQL 备份（最新 {max(len(kept_pg), 1)} 份）",
                description="灾难恢复底线，始终保留最新备份。",
                bytes=sum(entry.stat().st_size for entry in kept_pg), count=len(kept_pg),
            ),
            CleanupItem(
                key="t3_pg_migration",
                title="PostgreSQL 迁移证据",
                description="postgresql-migration/ 为数据库迁移 gate 证据，删除将破坏迁移审计链。",
                bytes=self._dir_size(self._root / "postgresql-migration"), count=1,
            ),
            CleanupItem(
                key="t3_runtime_files",
                title="运行时文件",
                description="axiom.db、postgresql.env、storage/state.json、skills/、projects/ 为系统运行依赖。",
                bytes=(
                    ((self._root / "axiom.db").stat().st_size if (self._root / "axiom.db").is_file() else 0)
                    + self._dir_size(self._root / "skills")
                    + self._dir_size(self._root / "projects")
                ),
                count=1,
            ),
        ])

        plan = CleanupPlan(
            generated_at=utcnow(),
            data_dir=str(self._root),
            refreshing=False,
            tier1=tier1, tier2=tier2, tier3=tier3,
        )
        index = {
            "lease_index": lease_index,
            "context_sizes": context_tasks,
            "artifact_sizes": artifact_tasks,
        }
        return plan, index

    def _read_json_file(self, rel_path: Path) -> dict | None:
        try:
            payload = json.loads((self._root / rel_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def _json_age(self, generated_at: object) -> timedelta:
        try:
            parsed = datetime.fromisoformat(str(generated_at).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return CLEANUP_PLAN_TTL + timedelta(days=1)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - parsed
