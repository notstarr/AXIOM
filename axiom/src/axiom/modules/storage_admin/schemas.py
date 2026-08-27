from __future__ import annotations

from pydantic import BaseModel


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
