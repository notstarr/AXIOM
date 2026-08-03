from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import and_, delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine

from axiom.modules.graph.tables import intents
from axiom.modules.reason.tables import reason_runs
from axiom.modules.tasks.tables import tasks
from axiom.modules.token_usage.schemas import (
    TaskTokenUsage,
    TokenUsageBatchCommand,
    TokenUsageBatchResult,
    TokenUsageDashboard,
    TokenUsageModel,
    TokenUsageRecordInput,
    TokenUsageSeriesPoint,
    TokenUsageSourceInput,
    TokenUsageSummary,
)
from axiom.modules.token_usage.tables import llm_token_usage, llm_token_usage_sources
from axiom.shared.clock import format_utc, utcnow
from axiom.shared.errors import LeaseInactiveError, NotFoundError


RANGE_DURATIONS: dict[str, timedelta | None] = {
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "3m": timedelta(days=90),
    "6m": timedelta(days=180),
    "1y": timedelta(days=365),
    "all": None,
}


class TokenUsageService:
    """Persist and aggregate token usage without reading prompt contents."""

    def __init__(self, engine: Engine):
        self.engine = engine

    def record_intent_batch(
        self,
        *,
        task_id: str,
        intent_id: str,
        command: TokenUsageBatchCommand,
    ) -> TokenUsageBatchResult:
        with self.engine.begin() as conn:
            row = conn.execute(
                select(
                    intents.c.lease_id,
                    intents.c.status,
                    intents.c.kind,
                    intents.c.worker,
                ).where(intents.c.task_id == task_id, intents.c.id == intent_id)
            ).mappings().one_or_none()
            if row is None:
                raise NotFoundError(f"Intent not found: {task_id}/{intent_id}")
            if row["lease_id"] != command.lease_id or row["status"] not in {
                "claimed",
                "running",
                "concluding",
            }:
                raise LeaseInactiveError("Intent lease is not active")
            return self._record_batch(
                conn,
                task_id=task_id,
                intent_id=intent_id,
                reason_run_id=None,
                lease_id=command.lease_id,
                phase=str(row["kind"] or "unknown"),
                worker=str(row["worker"] or ""),
                command=command,
                source="live",
            )

    def record_reason_batch(
        self,
        *,
        task_id: str,
        reason_run_id: str,
        command: TokenUsageBatchCommand,
    ) -> TokenUsageBatchResult:
        with self.engine.begin() as conn:
            row = conn.execute(
                select(
                    reason_runs.c.lease_id,
                    reason_runs.c.status,
                    reason_runs.c.worker,
                ).where(reason_runs.c.task_id == task_id, reason_runs.c.id == reason_run_id)
            ).mappings().one_or_none()
            if row is None:
                raise NotFoundError(f"ReasonRun not found: {task_id}/{reason_run_id}")
            if row["lease_id"] != command.lease_id or row["status"] not in {
                "claimed",
                "running",
                "concluding",
            }:
                raise LeaseInactiveError("ReasonRun lease is not active")
            return self._record_batch(
                conn,
                task_id=task_id,
                intent_id=None,
                reason_run_id=reason_run_id,
                lease_id=command.lease_id,
                phase="reason",
                worker=str(row["worker"] or ""),
                command=command,
                source="live",
            )

    def record_backfill_batch(
        self,
        *,
        task_id: str,
        intent_id: str | None,
        reason_run_id: str | None,
        lease_id: str,
        phase: str,
        worker: str,
        records: Iterable[TokenUsageRecordInput],
        sources: Iterable[TokenUsageSourceInput],
        source: str = "backfill",
    ) -> TokenUsageBatchResult:
        if source not in {"backfill", "legacy"}:
            raise ValueError("backfill source must be backfill or legacy")
        with self.engine.begin() as conn:
            task_exists = conn.execute(select(tasks.c.id).where(tasks.c.id == task_id)).scalar_one_or_none()
            if task_exists is None:
                raise NotFoundError(f"Task not found: {task_id}")
            command = TokenUsageBatchCommand(
                lease_id=lease_id,
                records=list(records),
                sources=list(sources),
            )
            return self._record_batch(
                conn,
                task_id=task_id,
                intent_id=intent_id,
                reason_run_id=reason_run_id,
                lease_id=lease_id,
                phase=phase if phase in {"bootstrap", "explore", "reason"} else "unknown",
                worker=worker,
                command=command,
                source=source,
            )

    def _record_batch(
        self,
        conn,
        *,
        task_id: str,
        intent_id: str | None,
        reason_run_id: str | None,
        lease_id: str,
        phase: str,
        worker: str,
        command: TokenUsageBatchCommand,
        source: str,
    ) -> TokenUsageBatchResult:
        inserted = 0
        duplicate = 0
        for record in command.records:
            source_key = usage_source_key(
                task_id=task_id,
                intent_id=intent_id,
                reason_run_id=reason_run_id,
                lease_id=lease_id,
                record=record,
            )
            result = conn.execute(
                llm_token_usage.insert().prefix_with("OR IGNORE").values(
                    source_key=source_key,
                    task_id=task_id,
                    intent_id=intent_id,
                    reason_run_id=reason_run_id,
                    lease_id=lease_id,
                    phase=phase,
                    worker=worker,
                    provider=record.provider,
                    model=record.model,
                    requested_model=record.requested_model,
                    session_id=record.session_id,
                    session_file=record.session_file,
                    session_entry_id=record.session_entry_id,
                    usage_kind=record.usage_kind,
                    occurred_at=record.occurred_at,
                    input_tokens=record.input_tokens,
                    cache_read_tokens=record.cache_read_tokens,
                    cache_write_tokens=record.cache_write_tokens,
                    output_tokens=record.output_tokens,
                    reasoning_tokens=record.reasoning_tokens,
                    reported_total_tokens=record.reported_total_tokens,
                    source=source,
                    created_at=utcnow(),
                )
            )
            if result.rowcount:
                inserted += 1
            else:
                duplicate += 1

        sources_updated = 0
        for source_record in command.sources:
            # A backfill can observe a lease before its run directory exists.
            # Remove that provisional marker once the real session source is
            # available, otherwise the dashboard would stay "incomplete"
            # forever even after a later retry produced a valid session.
            if source_record.session_file != "sessions/missing.jsonl":
                conn.execute(
                    delete(llm_token_usage_sources).where(
                        llm_token_usage_sources.c.task_id == task_id,
                        llm_token_usage_sources.c.intent_id == intent_id
                        if intent_id is not None
                        else llm_token_usage_sources.c.intent_id.is_(None),
                        llm_token_usage_sources.c.reason_run_id == reason_run_id
                        if reason_run_id is not None
                        else llm_token_usage_sources.c.reason_run_id.is_(None),
                        llm_token_usage_sources.c.lease_id == lease_id,
                        llm_token_usage_sources.c.session_file == "sessions/missing.jsonl",
                    )
                )
            source_key = source_status_key(
                task_id=task_id,
                intent_id=intent_id,
                reason_run_id=reason_run_id,
                lease_id=lease_id,
                session_file=source_record.session_file,
            )
            statement = sqlite_insert(llm_token_usage_sources).values(
                source_key=source_key,
                task_id=task_id,
                intent_id=intent_id,
                reason_run_id=reason_run_id,
                lease_id=lease_id,
                session_file=source_record.session_file,
                session_id=source_record.session_id,
                status=source_record.status,
                error=source_record.error,
                source=source,
                scanned_at=utcnow(),
            )
            statement = statement.on_conflict_do_update(
                index_elements=[llm_token_usage_sources.c.source_key],
                set_={
                    "session_id": statement.excluded.session_id,
                    "status": statement.excluded.status,
                    "error": statement.excluded.error,
                    "source": statement.excluded.source,
                    "scanned_at": statement.excluded.scanned_at,
                },
            )
            conn.execute(statement)
            sources_updated += 1
        return TokenUsageBatchResult(
            inserted=inserted,
            duplicate=duplicate,
            sources_updated=sources_updated,
        )

    def dashboard(
        self,
        *,
        token_range: str = "all",
        model: str = "all",
    ) -> TokenUsageDashboard:
        token_range = normalize_token_range(token_range)
        now = datetime.now(timezone.utc)
        with self.engine.connect() as conn:
            rows = self._usage_rows(
                conn,
                token_range=token_range,
                model=model,
                now=now,
            )
            task_ids = {str(row["task_id"]) for row in rows}
            incomplete_by_task = self._incomplete_counts(conn)
            task_ids.update(incomplete_by_task)
            task_rows = {}
            if task_ids:
                task_rows = {
                    str(row["id"]): row
                    for row in conn.execute(select(tasks).where(tasks.c.id.in_(task_ids))).mappings()
                }
            incomplete = sum(incomplete_by_task.values())
            models = self._available_models(conn)
        by_task: dict[str, dict[str, Any]] = {}
        for row in rows:
            task_id = str(row["task_id"])
            bucket = by_task.setdefault(task_id, _new_bucket())
            _add_row(bucket, row)
        task_usage: list[TaskTokenUsage] = []
        for task_id in sorted(task_ids):
            bucket = by_task.get(task_id, _new_bucket())
            task = task_rows.get(task_id, {})
            summary = _finalize_bucket(
                bucket,
                incomplete_sources=incomplete_by_task.get(task_id, 0),
            )
            task_usage.append(
                TaskTokenUsage(
                    task_id=task_id,
                    title=str(task.get("title") or task_id),
                    status=str(task.get("status") or ""),
                    **summary,
                )
            )
        task_usage.sort(key=lambda item: (-item.total, item.task_id))
        summary = _finalize_bucket(_aggregate_rows(rows), incomplete_sources=incomplete)
        return TokenUsageDashboard(
            token_usage=TokenUsageSummary(**summary),
            token_by_task=task_usage,
            token_series=self._series(rows, token_range, now=now),
            available_models=models,
            token_range=token_range,  # type: ignore[arg-type]
            token_model=model or "all",
        )

    def rankings(
        self,
        *,
        task_id: str,
        token_range: str = "all",
        model: str = "all",
    ) -> dict[str, Any]:
        token_range = normalize_token_range(token_range)
        now = datetime.now(timezone.utc)
        with self.engine.connect() as conn:
            rows = self._usage_rows(
                conn,
                task_id=task_id,
                token_range=token_range,
                model=model,
                now=now,
            )
            incomplete = self._incomplete_count(conn, task_id=task_id)
            models = self._available_models(conn, task_id=task_id)
        totals = _finalize_bucket(_aggregate_rows(rows), incomplete_sources=incomplete)
        by_worker = self._group_rows(rows, "worker", incomplete)
        by_phase = self._group_rows(rows, "phase", incomplete)
        by_model = self._group_rows(rows, "model_key", incomplete)
        for item in by_phase:
            item["worker_count"] = len(item.pop("workers", []))
        return {
            "totals": totals,
            "by_worker": by_worker,
            "by_phase": by_phase,
            "by_model": by_model,
            "available_models": [item.model_dump(mode="json") for item in models],
            "token_range": token_range,
            "token_model": model or "all",
            "notes": [
                "Token 统计来自 Pi Session JSONL；reasoning 已包含在 output 中，不重复计入。"
            ] + ([f"有 {incomplete} 个 Session 来源不完整，统计未猜测缺失数据。"] if incomplete else []),
        }

    def _usage_rows(
        self,
        conn,
        *,
        task_id: str | None = None,
        token_range: str,
        model: str = "all",
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        query = select(llm_token_usage)
        conditions = []
        if task_id:
            conditions.append(llm_token_usage.c.task_id == task_id)
        start = range_start(token_range, now=now)
        if start is not None:
            conditions.append(llm_token_usage.c.occurred_at >= start)
        model_condition = model_condition_for(model)
        if model_condition is not None:
            conditions.append(model_condition)
        if conditions:
            query = query.where(and_(*conditions))
        query = query.order_by(llm_token_usage.c.occurred_at, llm_token_usage.c.source_key)
        return [dict(row) for row in conn.execute(query).mappings()]

    def _incomplete_count(self, conn, *, task_id: str | None = None) -> int:
        query = select(func.count()).select_from(llm_token_usage_sources).where(
            llm_token_usage_sources.c.status == "incomplete"
        )
        if task_id:
            query = query.where(llm_token_usage_sources.c.task_id == task_id)
        return int(conn.execute(query).scalar_one() or 0)

    def _incomplete_counts(self, conn) -> dict[str, int]:
        query = (
            select(
                llm_token_usage_sources.c.task_id,
                func.count().label("count"),
            )
            .where(llm_token_usage_sources.c.status == "incomplete")
            .group_by(llm_token_usage_sources.c.task_id)
        )
        return {
            str(row["task_id"]): int(row["count"] or 0)
            for row in conn.execute(query).mappings()
        }

    def _available_models(self, conn, *, task_id: str | None = None) -> list[TokenUsageModel]:
        query = select(
            llm_token_usage.c.provider,
            llm_token_usage.c.model,
            func.count().label("calls"),
        ).where(
            (llm_token_usage.c.provider != "") | (llm_token_usage.c.model != "")
        )
        if task_id:
            query = query.where(llm_token_usage.c.task_id == task_id)
        query = query.group_by(llm_token_usage.c.provider, llm_token_usage.c.model).order_by(
            llm_token_usage.c.provider,
            llm_token_usage.c.model,
        )
        return [
            TokenUsageModel(
                key=_model_key(row["provider"], row["model"]),
                provider=str(row["provider"] or ""),
                model=str(row["model"] or ""),
                calls=int(row["calls"] or 0),
            )
            for row in conn.execute(query).mappings()
        ]

    def _series(
        self,
        rows: list[dict[str, Any]],
        token_range: str,
        *,
        now: datetime | None = None,
    ) -> list[TokenUsageSeriesPoint]:
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            occurred = _parse_timestamp(row.get("occurred_at"))
            if occurred is None:
                continue
            bucket_name = _bucket_name(occurred, token_range)
            bucket = grouped.setdefault(bucket_name, _new_bucket())
            _add_row(bucket, row)
        bucket_names = _fixed_bucket_names(token_range, now=now)
        if not bucket_names:
            bucket_names = _all_bucket_names(grouped, now=now)
        return [
            TokenUsageSeriesPoint(
                bucket=name,
                **_series_bucket_values(grouped.get(name, _new_bucket())),
            )
            for name in bucket_names
        ]

    @staticmethod
    def _group_rows(rows: list[dict[str, Any]], key: str, incomplete: int) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            if key == "model_key":
                group_key = _model_key(row.get("provider"), row.get("model")) or "未知模型"
            else:
                group_key = str(row.get(key) or "未标记")
            bucket = grouped.setdefault(group_key, _new_bucket())
            _add_row(bucket, row)
            if key == "worker":
                bucket["phases"].add(str(row.get("phase") or "unknown"))
            if key == "phase":
                bucket["workers"].add(str(row.get("worker") or "未标记"))
            if key == "model_key":
                bucket["phases"].add(str(row.get("phase") or "unknown"))
                bucket["workers"].add(str(row.get("worker") or "未标记"))
        result: list[dict[str, Any]] = []
        for name, bucket in grouped.items():
            item = _finalize_bucket(bucket, incomplete_sources=incomplete)
            if key == "worker":
                item["worker"] = name
                item["name"] = name
                item["phases"] = sorted(bucket["phases"])
            elif key == "phase":
                item["phase"] = name
                item["name"] = name
                item["workers"] = sorted(bucket["workers"])
            else:
                provider, model = _split_model_key(name)
                item["name"] = name
                item["model_key"] = name
                item["provider"] = provider
                item["model"] = model
                item["phases"] = sorted(bucket["phases"])
                item["workers"] = sorted(bucket["workers"])
            result.append(item)
        result.sort(key=lambda item: (-int(item.get("total", 0)), str(item.get("name", ""))))
        return result


def usage_source_key(
    *,
    task_id: str,
    intent_id: str | None,
    reason_run_id: str | None,
    lease_id: str,
    record: TokenUsageRecordInput,
) -> str:
    """Return the stable identity of one usage entry in a Pi Session.

    Ownership metadata is deliberately not part of this key.  The same
    Session can be observed once by the live tailer and again by backfill (or
    be resolved through a different Context revision); those observations
    must converge on one durable row.  The Session file and Session ID are
    the source identity, while the entry ID plus usage kind distinguish the
    usage-bearing entries inside it.

    The owner arguments remain in the signature for callers that used the
    pre-persistence helper and for readability at call sites, but are not
    included in the deduplication identity.
    """
    del task_id, intent_id, reason_run_id, lease_id
    raw = "|".join(
        (
            record.session_file,
            record.session_id,
            record.session_entry_id,
            record.usage_kind,
        )
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def source_status_key(
    *,
    task_id: str,
    intent_id: str | None,
    reason_run_id: str | None,
    lease_id: str,
    session_file: str,
) -> str:
    owner = intent_id or reason_run_id or "unknown"
    raw = "|".join((task_id, owner, lease_id, session_file))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def normalize_token_range(value: str | None) -> str:
    normalized = (value or "all").strip().lower()
    if normalized not in RANGE_DURATIONS:
        return "all"
    return normalized


def range_start(token_range: str, *, now: datetime | None = None) -> str | None:
    """Return the inclusive start of the first displayed time bucket.

    The series is bucketed by calendar days, ISO weeks, or calendar months.
    Using a rolling timestamp here would include records in a bucket that is
    not present in the chart (for example, yesterday afternoon in a 7-day
    view).  Aligning the query window to the bucket boundary keeps totals and
    the chart consistent.
    """

    normalized = normalize_token_range(token_range)
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if normalized == "all":
        return None
    if normalized in {"7d", "30d"}:
        days = 7 if normalized == "7d" else 30
        start_date = current.date() - timedelta(days=days - 1)
        return format_utc(datetime.combine(start_date, datetime.min.time(), tzinfo=timezone.utc))
    if normalized in {"3m", "6m"}:
        duration = RANGE_DURATIONS[normalized]
        assert duration is not None
        start_date = (current - duration).date()
        start_date -= timedelta(days=start_date.weekday())
        return format_utc(datetime.combine(start_date, datetime.min.time(), tzinfo=timezone.utc))
    first_month = _shift_month(date(current.year, current.month, 1), -11)
    return format_utc(datetime.combine(first_month, datetime.min.time(), tzinfo=timezone.utc))


def model_condition_for(model: str | None):
    normalized = (model or "all").strip()
    if not normalized or normalized == "all":
        return None
    if "/" in normalized:
        provider, model_name = normalized.split("/", 1)
        return and_(
            llm_token_usage.c.provider == provider,
            llm_token_usage.c.model == model_name,
        )
    return llm_token_usage.c.model == normalized


def _new_bucket() -> dict[str, Any]:
    return {
        "input": 0,
        "input_uncached": 0,
        "cache_read": 0,
        "cache_write": 0,
        "output": 0,
        "reasoning": 0,
        "total": 0,
        "calls": 0,
        "phases": set(),
        "workers": set(),
    }


def _add_row(bucket: dict[str, Any], row: dict[str, Any]) -> None:
    input_tokens = int(row.get("input_tokens") or 0)
    cache_read = int(row.get("cache_read_tokens") or 0)
    cache_write = int(row.get("cache_write_tokens") or 0)
    output = int(row.get("output_tokens") or 0)
    bucket["input"] += input_tokens
    bucket["input_uncached"] += input_tokens
    bucket["cache_read"] += cache_read
    bucket["cache_write"] += cache_write
    bucket["output"] += output
    bucket["reasoning"] += int(row.get("reasoning_tokens") or 0)
    bucket["total"] += input_tokens + cache_read + cache_write + output
    bucket["calls"] += 1


def _aggregate_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    bucket = _new_bucket()
    for row in rows:
        _add_row(bucket, row)
    return bucket


def _finalize_bucket(bucket: dict[str, Any], *, incomplete_sources: int = 0) -> dict[str, Any]:
    prompt_tokens = int(bucket["input_uncached"] + bucket["cache_read"] + bucket["cache_write"])
    result = {
        "input": int(bucket["input"]),
        "input_uncached": int(bucket["input_uncached"]),
        "input_tokens": int(bucket["input_uncached"]),
        "cache_read": int(bucket["cache_read"]),
        "cache_read_tokens": int(bucket["cache_read"]),
        "cache_write": int(bucket["cache_write"]),
        "cache_write_tokens": int(bucket["cache_write"]),
        "output": int(bucket["output"]),
        "output_tokens": int(bucket["output"]),
        "reasoning": int(bucket["reasoning"]),
        "reasoning_tokens": int(bucket["reasoning"]),
        "total": int(bucket["total"]),
        "total_tokens": int(bucket["total"]),
        "calls": int(bucket["calls"]),
        "cache_hit_rate": (bucket["cache_read"] / prompt_tokens) if prompt_tokens else None,
        "complete": incomplete_sources == 0,
        "incomplete_sources": int(incomplete_sources),
    }
    return result


def _series_bucket_values(bucket: dict[str, Any]) -> dict[str, int]:
    return {
        "input": int(bucket["input"]),
        "input_uncached": int(bucket["input_uncached"]),
        "cache_read": int(bucket["cache_read"]),
        "cache_write": int(bucket["cache_write"]),
        "output": int(bucket["output"]),
        "total": int(bucket["total"]),
    }


def _parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _bucket_name(value: datetime, token_range: str) -> str:
    token_range = normalize_token_range(token_range)
    if token_range in {"7d", "30d"}:
        return value.date().isoformat()
    if token_range in {"3m", "6m"}:
        monday = value.date() - timedelta(days=value.weekday())
        return monday.isoformat()
    return f"{value.year:04d}-{value.month:02d}"


def _fixed_bucket_names(
    token_range: str,
    *,
    now: datetime | None = None,
) -> list[str]:
    token_range = normalize_token_range(token_range)
    if token_range == "all":
        return []
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if token_range in {"7d", "30d"}:
        days = 7 if token_range == "7d" else 30
        start = current.date() - timedelta(days=days - 1)
        return [(start + timedelta(days=offset)).isoformat() for offset in range(days)]
    if token_range in {"3m", "6m"}:
        start = range_start(token_range, now=current)
        assert start is not None
        first = datetime.fromisoformat(start.replace("Z", "+00:00")).date()
        last = current.date() - timedelta(days=current.weekday())
        names: list[str] = []
        cursor = first
        while cursor <= last:
            names.append(cursor.isoformat())
            cursor += timedelta(days=7)
        return names
    months = 12
    first_month = _shift_month(date(current.year, current.month, 1), -(months - 1))
    return [
        f"{month.year:04d}-{month.month:02d}"
        for month in (_shift_month(first_month, offset) for offset in range(months))
    ]


def _all_bucket_names(
    grouped: dict[str, dict[str, Any]],
    *,
    now: datetime,
) -> list[str]:
    """Fill monthly buckets for the all-time chart, including empty months."""

    names = sorted(name for name in grouped if len(name) == 7 and name[4] == "-")
    if not names:
        return []
    first_year, first_month = (int(part) for part in names[0].split("-"))
    first = date(first_year, first_month, 1)
    last = date(now.year, now.month, 1)
    if names[-1] > f"{last.year:04d}-{last.month:02d}":
        last_year, last_month = (int(part) for part in names[-1].split("-"))
        last = date(last_year, last_month, 1)
    result: list[str] = []
    cursor = first
    while cursor <= last:
        result.append(f"{cursor.year:04d}-{cursor.month:02d}")
        cursor = _shift_month(cursor, 1)
    return result


def _shift_month(value: date, offset: int) -> date:
    month_index = value.year * 12 + value.month - 1 + offset
    return date(month_index // 12, month_index % 12 + 1, 1)


def _model_key(provider: Any, model: Any) -> str:
    provider_text = str(provider or "").strip()
    model_text = str(model or "").strip()
    if provider_text and model_text:
        return f"{provider_text}/{model_text}"
    return model_text or provider_text


def _split_model_key(value: str) -> tuple[str, str]:
    if "/" not in value:
        return "", value
    return tuple(value.split("/", 1))  # type: ignore[return-value]
