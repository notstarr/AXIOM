from __future__ import annotations

from pydantic import BaseModel, Field


class DiskStats(BaseModel):
    total_bytes: int
    free_bytes: int


class StorageCategory(BaseModel):
    key: str
    path: str
    bytes: int
    items: int


class DatabaseTableStat(BaseModel):
    name: str
    total_bytes: int
    rows: int


class DatabaseStats(BaseModel):
    total_bytes: int
    tables: list[DatabaseTableStat]


class LogFileStat(BaseModel):
    name: str
    bytes: int


class LogStats(BaseModel):
    files: list[LogFileStat]
    archived_count: int
    archived_bytes: int
    keep_days: int
    last_rotate_at: str | None = None


class StorageOverview(BaseModel):
    generated_at: str
    data_dir: str
    total_bytes: int
    disk: DiskStats
    categories: list[StorageCategory]
    database: DatabaseStats
    logs: LogStats
    refreshing: bool = False


class LogRotateItem(BaseModel):
    file: str
    archived_to: str
    original_bytes: int


class LogRotateResult(BaseModel):
    rotated: list[LogRotateItem]
    purged_archives: list[str]
    purged_archives_bytes: int
    bytes_freed_total: int
    actor: str


# ── 清理中心（三档） ──

class CleanupItem(BaseModel):
    key: str
    title: str
    description: str
    bytes: int = 0
    count: int = 0
    warning: str = ""
    default_selected: bool = False


class CleanupPlan(BaseModel):
    generated_at: str
    data_dir: str
    refreshing: bool = False
    tier1: list[CleanupItem]
    tier2: list[CleanupItem]
    tier3: list[CleanupItem]


class CleanupPreviewTarget(BaseModel):
    path: str
    bytes: int
    kind: str


class CleanupPreviewItem(BaseModel):
    key: str
    title: str
    targets: list[CleanupPreviewTarget]
    total_bytes: int
    protected_count: int = 0
    warning: str = ""


class CleanupPreviewRequest(BaseModel):
    keys: list[str]


class CleanupPreviewResponse(BaseModel):
    generated_at: str
    items: list[CleanupPreviewItem]


class CleanupExecuteRequest(BaseModel):
    keys: list[str]
    excluded: list[str] = []
    actor: str = "human.frontend"


class CleanupRemovedItem(BaseModel):
    key: str
    path: str
    bytes: int


class CleanupSkippedItem(BaseModel):
    key: str
    path: str
    reason: str


class CleanupExecuteResult(BaseModel):
    freed_bytes: int
    removed: list[CleanupRemovedItem]
    skipped: list[CleanupSkippedItem]
    actor: str


class ContextRetentionTarget(BaseModel):
    path: str
    task_id: str
    revision: int | None = None
    bytes: int
    kind: str
    reason: str
    phase: str | None = None


class ContextRetentionPlan(BaseModel):
    generated_at: str
    targets: list[ContextRetentionTarget]
    database_records: int = 0
    reclaimable_count: int = 0
    protected_count: int
    orphan_count: int
    total_bytes: int


class ContextRetentionPreviewRequest(BaseModel):
    limit: int = Field(default=100, ge=1, le=10_000)


class ContextRetentionRunRequest(BaseModel):
    dry_run: bool = True
    actor: str = "human.frontend"
    max_revisions: int | None = Field(default=None, ge=1, le=10_000)
    max_bytes: int | None = Field(default=None, ge=1)


class ContextRetentionRunResult(BaseModel):
    generated_at: str
    dry_run: bool
    paused: bool
    removed: list[ContextRetentionTarget]
    skipped: list[dict[str, str]]
    freed_bytes: int
    protected_count: int
    orphan_count: int
