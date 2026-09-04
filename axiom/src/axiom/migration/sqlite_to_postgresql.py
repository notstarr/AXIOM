from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import Boolean, Engine, UniqueConstraint, func, select, text

from axiom.modules.mcp.schema_order import annotate_input_schema
from axiom.shared.database import (
    AXIOM_SCHEMA,
    JSONText,
    UTCDateTimeText,
    create_database_engine,
    metadata,
    schema_is_ready,
)
from axiom.shared.model_registry import ALL_TABLES


LEGACY_SQLITE_REVISION = "20260805_0035"
# The importer must validate against the current PostgreSQL schema head.  Keep
# this aligned with the latest migration whenever a schema change is added;
# otherwise a database that has successfully upgraded cannot accept a legacy
# SQLite import.
POSTGRESQL_TARGET_REVISION = "20260902_pg0014"
DEFAULT_BATCH_SIZE = 1_000

# A 20260805 SQLite export predates the independent Plan runtime.  These
# tables are represented as empty target tables during import; all other
# historical rows remain byte-for-byte migratable.
LEGACY_SQLITE_OMIT_TABLES = frozenset(
    {"plan_runs", "plan_run_sources", "plan_workspaces"}
)

LEGACY_SQLITE_COLUMN_DEFAULTS: dict[str, dict[str, Any]] = {
    "audit_events": {"plan_run_id": None},
    "context_revisions": {"plan_run_id": None},
    # The legacy 20260805 SQLite export already contains the Review owner,
    # but predates the independent Plan runtime.  Treat Plan ownership as a
    # target-only nullable column during import, just like the other newly
    # introduced owner references.
    "llm_token_usage": {"plan_run_id": None},
    "llm_token_usage_sources": {"plan_run_id": None},
    # The source SQLite export may predate the independent Plan pool.  The
    # target schema backfills it to the compatibility value (inherit the
    # ordinary Task Worker pool).
    "tasks": {"plan_worker_pool_json": "[]"},
    # The source SQLite export predates the vulnerability human-review
    # toggle.  Imported rows keep the unreviewed default.
    "vulnerabilities": {
        "human_reviewed": False,
        "human_reviewed_at": None,
        "human_reviewed_by": None,
    },
    "intents": {
        "human_intervention_required": False,
        "human_intervention_reason": "",
        "human_intervention_marked_at": None,
        "priority": 50,
        "assessment_status": "open",
        "assessment_reason": "",
        "assessed_at": None,
        "dedup_fingerprint": "",
        "plan_metadata_json": "{}",
    },
}

# Columns present only in the frozen SQLite export.  They are validated as
# part of the source shape and then ignored when rows are normalized for the
# current PostgreSQL schema.
LEGACY_SQLITE_SOURCE_ONLY_COLUMNS: dict[str, set[str]] = {
    "reason_runs": {"target_open_intents", "max_new_intents"},
}


def _migration_tables():
    registered_names = {table.name for table in ALL_TABLES}
    return tuple(
        table
        for table in metadata.sorted_tables
        if table.name in registered_names
    )


@dataclass(slots=True)
class TableMigrationResult:
    table: str
    source_count: int
    target_count: int
    source_sha256: str
    target_sha256: str

    @property
    def matches(self) -> bool:
        return (
            self.source_count == self.target_count
            and self.source_sha256 == self.target_sha256
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "source_count": self.source_count,
            "target_count": self.target_count,
            "source_sha256": self.source_sha256,
            "target_sha256": self.target_sha256,
            "matches": self.matches,
        }


@dataclass(slots=True)
class CounterMigrationResult:
    scope: str
    kind: str
    source_next_value: int
    required_next_value: int
    target_next_value: int
    max_entity_id: str

    @property
    def adjusted(self) -> bool:
        return self.source_next_value != self.target_next_value

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "kind": self.kind,
            "source_next_value": self.source_next_value,
            "required_next_value": self.required_next_value,
            "target_next_value": self.target_next_value,
            "max_entity_id": self.max_entity_id,
            "adjusted": self.adjusted,
        }


@dataclass(slots=True)
class SQLiteMigrationReport:
    source_path: str
    target_database: str
    source_revision: str
    target_revision: str
    integrity_check: str
    foreign_key_violations: int
    duration_seconds: float
    artifact_total: int = 0
    artifact_existing: int = 0
    tables: list[TableMigrationResult] = field(default_factory=list)
    counters: list[CounterMigrationResult] = field(default_factory=list)

    @property
    def matches(self) -> bool:
        return (
            self.integrity_check == "ok"
            and self.foreign_key_violations == 0
            and all(item.matches for item in self.tables)
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_path": self.source_path,
            "target_database": self.target_database,
            "source_revision": self.source_revision,
            "target_revision": self.target_revision,
            "integrity_check": self.integrity_check,
            "foreign_key_violations": self.foreign_key_violations,
            "duration_seconds": round(self.duration_seconds, 3),
            "artifact_total": self.artifact_total,
            "artifact_existing": self.artifact_existing,
            "matches": self.matches,
            "tables": [item.as_dict() for item in self.tables],
            "counters": [item.as_dict() for item in self.counters],
        }

    def markdown(self) -> str:
        lines = [
            "# AXIOM SQLite → PostgreSQL 迁移报告",
            "",
            f"- Source: `{self.source_path}`",
            f"- Target: `{self.target_database}`",
            f"- SQLite revision: `{self.source_revision}`",
            f"- PostgreSQL revision: `{self.target_revision}`",
            f"- Integrity: `{self.integrity_check}`",
            f"- Foreign-key violations: `{self.foreign_key_violations}`",
            f"- Artifact files: `{self.artifact_existing}/{self.artifact_total}`",
            f"- Duration: `{self.duration_seconds:.3f}s`",
            f"- Result: `{'PASS' if self.matches else 'FAIL'}`",
            "",
            "| Table | Source | Target | Hash |",
            "|---|---:|---:|---|",
        ]
        lines.extend(
            f"| {item.table} | {item.source_count} | {item.target_count} | "
            f"{'OK' if item.matches else 'MISMATCH'} |"
            for item in self.tables
        )
        lines.extend(
            [
                "",
                "## ID Counters",
                "",
                "| Scope | Kind | Source next | Required next | Target next | Max entity |",
                "|---|---|---:|---:|---:|---|",
            ]
        )
        lines.extend(
            f"| {item.scope} | {item.kind} | {item.source_next_value} | "
            f"{item.required_next_value} | {item.target_next_value} | "
            f"{item.max_entity_id or '-'} |"
            for item in self.counters
        )
        return "\n".join(lines) + "\n"


def migrate_sqlite_to_postgresql(
    sqlite_path: str | Path,
    database_url: str,
    *,
    data_dir: str | Path | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> SQLiteMigrationReport:
    source_path = Path(sqlite_path).expanduser().resolve()
    if not source_path.is_file():
        raise ValueError(f"SQLite source does not exist: {source_path}")
    if batch_size < 1:
        raise ValueError("batch_size must be positive")

    started = time.monotonic()
    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    target = create_database_engine(
        database_url,
        application_name="axiom-sqlite-importer",
        pool_size=1,
        max_overflow=0,
    )
    try:
        source_revision, integrity, foreign_key_violations = _validate_source(source)
        target_revision = _validate_target(target)
        source_rows: dict[str, list[dict[str, Any]]] = {}

        tables = _migration_tables()
        for table in tables:
            _validate_source_table(source, table)
            rows = _read_source_rows(source, table)
            source_rows[table.name] = rows

        _normalize_legacy_todo_statuses(source_rows)

        counters = _synchronize_source_counters(source_rows)
        source_hashes = {
            table.name: _rows_hash(table, source_rows[table.name]) for table in tables
        }

        with target.begin() as conn:
            _require_empty_target(conn)
            for table in tables:
                rows = source_rows[table.name]
                for offset in range(0, len(rows), batch_size):
                    try:
                        conn.execute(table.insert(), rows[offset : offset + batch_size])
                    except Exception as exc:
                        detail = str(getattr(exc, "orig", exc)).splitlines()[0]
                        raise RuntimeError(
                            f"failed importing {table.name} rows {offset + 1}-"
                            f"{min(offset + batch_size, len(rows))}: "
                            f"{type(exc).__name__}: {detail}"
                        ) from exc
            results: list[TableMigrationResult] = []
            for table in tables:
                rows = [dict(row) for row in conn.execute(select(table)).mappings().all()]
                results.append(
                    TableMigrationResult(
                        table=table.name,
                        source_count=len(source_rows[table.name]),
                        target_count=len(rows),
                        source_sha256=source_hashes[table.name],
                        target_sha256=_rows_hash(table, rows),
                    )
                )
            invalid_constraints = conn.execute(
                text(
                    "SELECT count(*) FROM pg_constraint c "
                    "JOIN pg_namespace n ON n.oid = c.connamespace "
                    "WHERE n.nspname = :schema AND NOT c.convalidated"
                ),
                {"schema": AXIOM_SCHEMA},
            ).scalar_one()
            if invalid_constraints:
                raise RuntimeError(
                    f"PostgreSQL has {invalid_constraints} unvalidated AXIOM constraints"
                )
            mismatches = [item.table for item in results if not item.matches]
            if mismatches:
                raise RuntimeError(
                    "migration verification failed; transaction rolled back: "
                    + ", ".join(mismatches)
                )

            counter_table = next(table for table in tables if table.name == "id_counters")
            target_counters = {
                (str(row["scope"]), str(row["kind"])): int(row["next_value"])
                for row in conn.execute(select(counter_table)).mappings()
            }
            for item in counters:
                item.target_next_value = target_counters[(item.scope, item.kind)]
                if item.target_next_value < item.required_next_value:
                    raise RuntimeError(
                        "counter verification failed; transaction rolled back: "
                        f"{item.scope}/{item.kind}={item.target_next_value}, "
                        f"required {item.required_next_value}"
                    )

        artifact_total, artifact_existing = _artifact_stats(
            source_rows.get("artifacts", []),
            Path(data_dir).expanduser().resolve() if data_dir else None,
        )
        report = SQLiteMigrationReport(
            source_path=str(source_path),
            target_database=_safe_database_label(target),
            source_revision=source_revision,
            target_revision=target_revision,
            integrity_check=integrity,
            foreign_key_violations=foreign_key_violations,
            duration_seconds=time.monotonic() - started,
            artifact_total=artifact_total,
            artifact_existing=artifact_existing,
            tables=results,
            counters=counters,
        )
        with target.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.exec_driver_sql("ANALYZE")
        return report
    finally:
        source.close()
        target.dispose()


def _validate_source(conn: sqlite3.Connection) -> tuple[str, str, int]:
    integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    if integrity != "ok":
        raise ValueError(f"SQLite integrity_check failed: {integrity}")
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise ValueError(f"SQLite foreign_key_check returned {len(violations)} rows")
    row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    revision = str(row[0]) if row else ""
    if revision != LEGACY_SQLITE_REVISION:
        raise ValueError(
            f"SQLite revision must be {LEGACY_SQLITE_REVISION}, found {revision or 'none'}"
        )
    return revision, integrity, len(violations)


def _validate_target(engine: Engine) -> str:
    if not schema_is_ready(engine):
        raise ValueError("PostgreSQL schema is not ready; run `axiom db upgrade` first")
    with engine.connect() as conn:
        revision = conn.execute(
            text(f'SELECT version_num FROM "{AXIOM_SCHEMA}".alembic_version')
        ).scalar_one_or_none()
    if revision != POSTGRESQL_TARGET_REVISION:
        raise ValueError(
            f"PostgreSQL revision must be {POSTGRESQL_TARGET_REVISION}, "
            f"found {revision or 'none'}"
        )
    return str(revision)


def _validate_source_table(
    conn: sqlite3.Connection,
    table,
) -> None:
    table_name = table.name
    if table_name in LEGACY_SQLITE_OMIT_TABLES:
        return
    target_only_columns = set(LEGACY_SQLITE_COLUMN_DEFAULTS.get(table_name, {}))
    expected_columns = [
        column.name for column in table.columns if column.name not in target_only_columns
    ]
    expected_columns.extend(sorted(LEGACY_SQLITE_SOURCE_ONLY_COLUMNS.get(table_name, set())))
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    if exists is None:
        raise ValueError(f"SQLite source table is missing: {table_name}")
    actual = [row[1] for row in conn.execute(f'PRAGMA table_info("{table_name}")')]
    if set(actual) != set(expected_columns) or len(actual) != len(expected_columns):
        raise ValueError(
            f"SQLite table shape mismatch for {table_name}: "
            f"expected {expected_columns}, found {actual}"
        )
    actual_indexes: set[tuple[bool, tuple[str, ...]]] = set()
    for row in conn.execute(f'PRAGMA index_list("{table_name}")'):
        index_name = str(row[1])
        columns = tuple(
            str(item[2])
            for item in conn.execute(f'PRAGMA index_info("{index_name}")')
            if item[2] is not None
        )
        if columns:
            actual_indexes.add((bool(row[2]), columns))

    expected_indexes: set[tuple[bool, tuple[str, ...]]] = set()
    for constraint in table.constraints:
        if isinstance(constraint, UniqueConstraint):
            columns = tuple(column.name for column in constraint.columns)
            if not any(column in target_only_columns for column in columns):
                expected_indexes.add((True, columns))
    for index in table.indexes:
        columns = tuple(column.name for column in index.columns)
        if not any(column in target_only_columns for column in columns):
            expected_indexes.add((bool(index.unique), columns))
    missing = sorted(expected_indexes - actual_indexes, key=repr)
    if missing:
        raise ValueError(f"SQLite table index mismatch for {table_name}: missing {missing}")


def _read_source_rows(conn: sqlite3.Connection, table) -> list[dict[str, Any]]:
    if table.name in LEGACY_SQLITE_OMIT_TABLES:
        return []
    order = [column.name for column in table.primary_key.columns] or [
        column.name for column in table.columns
    ]
    order_sql = ", ".join(f'"{name}"' for name in order)
    rows = conn.execute(f'SELECT * FROM "{table.name}" ORDER BY {order_sql}').fetchall()
    return [_normalize_row_for_insert(table, dict(row)) for row in rows]


def _normalize_row_for_insert(table, row: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    legacy_defaults = LEGACY_SQLITE_COLUMN_DEFAULTS.get(table.name, {})
    for column in table.columns:
        value = row.get(column.name, legacy_defaults.get(column.name))
        if isinstance(column.type, Boolean) and value is not None:
            value = bool(value)
        elif isinstance(column.type, JSONText) and value is not None:
            parsed = json.loads(str(value))
            if table.name == "mcp_tools" and column.name == "input_schema_json":
                parsed = annotate_input_schema(parsed)
                value = json.dumps(parsed, ensure_ascii=False)
            else:
                value = str(value)
        elif isinstance(column.type, UTCDateTimeText):
            value = _normalize_timestamp(value)
        normalized[column.name] = value
    return normalized


def _normalize_legacy_todo_statuses(source_rows: dict[str, list[dict[str, Any]]]) -> None:
    """Map the legacy Todo lifecycle to the canonical four business states."""

    for row in source_rows.get("todos", []):
        status = str(row.get("status") or "")
        if status == "pending":
            row["status"] = "open"
        elif status == "active":
            row["status"] = "partial"
        if row.get("status") in {"partial", "blocked", "completed"} and not str(
            row.get("status_reason") or ""
        ).strip():
            row["status_reason"] = "从旧 Todo 状态迁移"
        if row.get("status") != "completed" and "completed_at" in row:
            row["completed_at"] = None


def _normalize_timestamp(value: Any) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


_GLOBAL_COUNTERS = {
    "archive_batch": ("archive_batches", "id", "ar"),
    "credential": ("credentials", "id", "cred_"),
    "credential_refresh": ("credential_refreshes", "id", "crf_"),
    "goal_cycle": ("task_goal_cycles", "id", "gc"),
    "goal_evaluation": ("goal_evaluations", "id", "ge"),
    "project": ("projects", "id", "prj_"),
    "reason_run": ("reason_runs", "id", "rr"),
    "reason_workspace": ("reason_workspaces", "id", "rw_"),
    "task": ("tasks", "id", "t_"),
    "vulnerability_review_run": ("vulnerability_review_runs", "id", "vr"),
}

_TASK_COUNTERS = {
    "artifact": ("artifacts", "id", "a"),
    "fact": ("facts", "id", "f"),
    "hint": ("task_hints", "id", "h"),
    "intent": ("intents", "id", "i"),
    "todo": ("todos", "id", "td"),
    "vulnerability": ("vulnerabilities", "id", "v"),
}


def _synchronize_source_counters(
    source_rows: dict[str, list[dict[str, Any]]],
) -> list[CounterMigrationResult]:
    counters = source_rows.get("id_counters", [])
    results: list[CounterMigrationResult] = []
    for counter in counters:
        scope = str(counter["scope"])
        kind = str(counter["kind"])
        source_next = int(counter["next_value"])
        required, max_entity_id = _required_next_value(source_rows, scope=scope, kind=kind)
        target_next = max(source_next, required)
        counter["next_value"] = target_next
        results.append(
            CounterMigrationResult(
                scope=scope,
                kind=kind,
                source_next_value=source_next,
                required_next_value=required,
                target_next_value=target_next,
                max_entity_id=max_entity_id,
            )
        )

    existing = {(str(row["scope"]), str(row["kind"])) for row in counters}
    required_keys: set[tuple[str, str]] = set()
    for kind in _GLOBAL_COUNTERS:
        required, _max_id = _required_next_value(source_rows, scope="global", kind=kind)
        if required > 1:
            required_keys.add(("global", kind))
    for task in source_rows.get("tasks", []):
        task_id = str(task["id"])
        for kind in (*_TASK_COUNTERS, "context_revision"):
            required, _max_id = _required_next_value(source_rows, scope=task_id, kind=kind)
            if required > 1:
                required_keys.add((task_id, kind))
    for project in source_rows.get("projects", []):
        project_id = str(project["id"])
        required, _max_id = _required_next_value(
            source_rows,
            scope=project_id,
            kind="knowledge",
        )
        if required > 1:
            required_keys.add((project_id, "knowledge"))
    missing = sorted(required_keys - existing)
    if missing:
        formatted = ", ".join(f"{scope}/{kind}" for scope, kind in missing)
        raise ValueError(f"SQLite source is missing required ID counters: {formatted}")
    return sorted(results, key=lambda item: (item.scope, item.kind))


def _required_next_value(
    source_rows: dict[str, list[dict[str, Any]]],
    *,
    scope: str,
    kind: str,
) -> tuple[int, str]:
    if scope == "global" and kind in _GLOBAL_COUNTERS:
        table_name, id_column, prefix = _GLOBAL_COUNTERS[kind]
        rows = source_rows.get(table_name, [])
    elif kind in _TASK_COUNTERS:
        table_name, id_column, prefix = _TASK_COUNTERS[kind]
        rows = [row for row in source_rows.get(table_name, []) if str(row["task_id"]) == scope]
    elif kind == "knowledge":
        table_name, id_column, prefix = ("project_knowledge", "id", "k")
        rows = [
            row
            for row in source_rows.get(table_name, [])
            if str(row["project_id"]) == scope
        ]
    elif kind == "context_revision":
        rows = [
            row
            for row in source_rows.get("context_revisions", [])
            if str(row["task_id"]) == scope
        ]
        maximum = max((int(row["revision"]) for row in rows), default=0)
        return maximum + 1, str(maximum) if maximum else ""
    else:
        return 1, ""

    maximum = 0
    maximum_id = ""
    pattern = re.compile(rf"^{re.escape(prefix)}(\d+)$")
    for row in rows:
        entity_id = str(row[id_column])
        match = pattern.fullmatch(entity_id)
        if match and int(match.group(1)) >= maximum:
            maximum = int(match.group(1))
            maximum_id = entity_id
    return maximum + 1, maximum_id


def _require_empty_target(conn) -> None:
    nonempty = []
    for table in _migration_tables():
        count = conn.execute(select(func.count()).select_from(table)).scalar_one()
        if count:
            nonempty.append(f"{table.name}={count}")
    if nonempty:
        raise ValueError("PostgreSQL target is not empty: " + ", ".join(nonempty))


def _rows_hash(table, rows: list[dict[str, Any]]) -> str:
    order = [column.name for column in table.primary_key.columns] or [
        column.name for column in table.columns
    ]
    normalized = [_canonical_row(table, row) for row in rows]
    normalized.sort(key=lambda row: tuple(_sort_value(row.get(name)) for name in order))
    digest = hashlib.sha256()
    for row in normalized:
        digest.update(
            json.dumps(
                row,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _canonical_row(table, row: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for column in table.columns:
        value = row.get(column.name)
        if isinstance(column.type, JSONText) and value is not None:
            value = json.loads(value) if isinstance(value, str) else value
        elif isinstance(column.type, UTCDateTimeText):
            value = _normalize_timestamp(value)
        elif isinstance(column.type, Boolean) and value is not None:
            value = bool(value)
        result[column.name] = value
    return result


def _sort_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _artifact_stats(rows: list[dict[str, Any]], data_dir: Path | None) -> tuple[int, int]:
    total = len(rows)
    if data_dir is None:
        return total, 0
    existing = sum(1 for row in rows if (data_dir / str(row["storage_key"])).is_file())
    return total, existing


def _safe_database_label(engine: Engine) -> str:
    url = engine.url
    host = url.host or ""
    port = f":{url.port}" if url.port else ""
    return f"{host}{port}/{url.database or ''}"
