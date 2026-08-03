from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from axiom.modules.audit.tables import audit_events
from axiom.modules.context.tables import context_revisions
from axiom.modules.graph.tables import intents
from axiom.modules.reason.tables import reason_runs, reason_workspaces
from axiom.modules.tasks.tables import tasks
from axiom.modules.token_usage.parser import normalize_usage, parse_session_file
from axiom.modules.token_usage.schemas import (
    TokenUsageBatchCommand,
    TokenUsageRecordInput,
    TokenUsageSourceInput,
)
from axiom.modules.token_usage.service import TokenUsageService
from axiom.modules.token_usage.tables import llm_token_usage


@dataclass(slots=True)
class BackfillReport:
    dry_run: bool
    task_ids: list[str]
    lease_directories: int = 0
    session_files: int = 0
    records_parsed: int = 0
    inserted: int = 0
    duplicate: int = 0
    complete_sources: int = 0
    incomplete_sources: int = 0
    missing_lease_sources: int = 0
    pending_batches: int = 0
    legacy_imported: int = 0
    skipped_unknown_tasks: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class OwnerMetadata:
    task_id: str
    intent_id: str | None
    reason_run_id: str | None
    lease_id: str
    phase: str
    worker: str
    recognized: bool = True
    resolution_error: str = ""


class TokenUsageBackfiller:
    def __init__(self, engine: Engine, data_dir: Path):
        self.engine = engine
        self.data_dir = data_dir.expanduser().resolve()
        self.service = TokenUsageService(engine)

    def run(
        self,
        *,
        task_ids: set[str] | None = None,
        dry_run: bool = False,
    ) -> BackfillReport:
        selected = set(task_ids or ())
        report = BackfillReport(dry_run=dry_run, task_ids=sorted(selected))
        seen_leases: set[tuple[str, str, str]] = set()
        session_usage_leases: set[tuple[str, str, str, str]] = set()
        runs_root = self.data_dir / "runs"
        if runs_root.is_dir():
            skipped_unknown_tasks: set[str] = set()
            for lease_dir in self._discover_lease_directories(runs_root):
                owner_dir = lease_dir.parent
                task_dir = owner_dir.parent
                task_id = task_dir.name
                if selected and task_id not in selected:
                    continue
                if not self._task_exists(task_id):
                    if task_id not in skipped_unknown_tasks:
                        skipped_unknown_tasks.add(task_id)
                        report.skipped_unknown_tasks += 1
                        report.errors.append(f"unknown_task:{task_id}")
                    continue
                report.lease_directories += 1
                owner = self._owner_metadata(task_id, owner_dir.name, lease_dir.name)
                seen_leases.add((task_id, owner_dir.name, lease_dir.name))
                self._scan_lease(
                    lease_dir,
                    owner,
                    report,
                    dry_run=dry_run,
                    session_usage_leases=session_usage_leases,
                )

        self._mark_missing_expected_leases(
            seen_leases,
            selected,
            report,
            dry_run=dry_run,
        )
        self._import_legacy_audit(
            selected,
            report,
            dry_run=dry_run,
            session_usage_leases=session_usage_leases,
        )
        return report

    @staticmethod
    def _discover_lease_directories(runs_root: Path) -> list[Path]:
        """Return only directories with recoverable Session or pending data.

        A run tree can also contain task-level artifacts and Lease-private
        working directories.  Walking every directory at depth three makes
        paths such as ``runs/t_150/artifacts/evidence`` look like a Lease and
        creates false incomplete-source markers.  The durable backfill inputs
        are the files named by the protocol: Session JSONL and the local
        pending spool.  Context revisions separately account for expected
        Leases whose run data is genuinely missing.
        """

        lease_dirs = {
            session_file.parent.parent
            for session_file in runs_root.glob("*/*/*/sessions/*.jsonl")
            if session_file.is_file()
        }
        lease_dirs.update(
            pending_file.parent.parent
            for pending_file in runs_root.glob("*/*/*/process/token-usage.pending.jsonl")
            if pending_file.is_file()
        )
        return sorted(lease_dirs)

    def _scan_lease(
        self,
        lease_dir: Path,
        owner: OwnerMetadata,
        report: BackfillReport,
        *,
        dry_run: bool,
        session_usage_leases: set[tuple[str, str, str, str]],
    ) -> None:
        session_dir = lease_dir / "sessions"
        session_files = sorted(session_dir.glob("*.jsonl")) if session_dir.is_dir() else []
        if not session_files:
            source = TokenUsageSourceInput(
                session_file="sessions/missing.jsonl",
                status="incomplete",
                error=_source_error(owner, "session_file_missing"),
            )
            report.incomplete_sources += 1
            if not dry_run:
                self._persist(owner, [], [source], report)
        for path in session_files:
            report.session_files += 1
            parsed = parse_session_file(path, session_file=f"sessions/{path.name}")
            report.records_parsed += len(parsed.records)
            if parsed.records:
                session_usage_leases.add(_owner_lease_key(owner))
            sources = [parsed.source] if parsed.source is not None else []
            if not owner.recognized:
                sources = [
                    source.model_copy(
                        update={
                            "status": "incomplete",
                            "error": _source_error(owner, source.error or "session_owner_unresolved"),
                        }
                    )
                    for source in sources
                ]
            for source in sources:
                if source.status == "complete":
                    report.complete_sources += 1
                else:
                    report.incomplete_sources += 1
            if not dry_run:
                self._persist(owner, parsed.records, sources, report)

        pending_path = lease_dir / "process" / "token-usage.pending.jsonl"
        if pending_path.is_file():
            try:
                lines = pending_path.read_text(encoding="utf-8").splitlines()
            except OSError as exc:
                report.errors.append(f"pending_read_error:{pending_path}:{exc.__class__.__name__}")
                return
            for index, line in enumerate(lines, start=1):
                if not line.strip():
                    continue
                try:
                    command = TokenUsageBatchCommand.model_validate_json(line)
                except ValueError:
                    report.errors.append(f"pending_invalid:{pending_path}:{index}")
                    continue
                report.pending_batches += 1
                report.records_parsed += len(command.records)
                if command.records:
                    session_usage_leases.add(_owner_lease_key(owner))
                if not owner.recognized:
                    command = command.model_copy(
                        update={
                            "sources": [
                                source.model_copy(
                                    update={
                                        "status": "incomplete",
                                        "error": _source_error(
                                            owner,
                                            source.error or "session_owner_unresolved",
                                        ),
                                    }
                                )
                                for source in command.sources
                            ]
                        }
                    )
                if not dry_run:
                    self._persist(owner, command.records, command.sources, report)

    def _persist(
        self,
        owner: OwnerMetadata,
        records,
        sources,
        report: BackfillReport,
        *,
        source: str = "backfill",
    ) -> None:
        records = list(records)
        sources = list(sources)
        for offset in range(0, len(records), 500):
            result = self.service.record_backfill_batch(
                task_id=owner.task_id,
                intent_id=owner.intent_id,
                reason_run_id=owner.reason_run_id,
                lease_id=owner.lease_id,
                phase=owner.phase,
                worker=owner.worker,
                records=records[offset : offset + 500],
                sources=[],
                source=source,
            )
            report.inserted += result.inserted
            report.duplicate += result.duplicate
        for offset in range(0, len(sources), 100):
            self.service.record_backfill_batch(
                task_id=owner.task_id,
                intent_id=owner.intent_id,
                reason_run_id=owner.reason_run_id,
                lease_id=owner.lease_id,
                phase=owner.phase,
                worker=owner.worker,
                records=[],
                sources=sources[offset : offset + 100],
                source=source,
            )

    def _mark_missing_expected_leases(
        self,
        seen_leases: set[tuple[str, str, str]],
        selected: set[str],
        report: BackfillReport,
        *,
        dry_run: bool,
    ) -> None:
        query = select(context_revisions).where(context_revisions.c.lease_id.is_not(None))
        if selected:
            query = query.where(context_revisions.c.task_id.in_(selected))
        with self.engine.connect() as conn:
            rows = conn.execute(query).mappings().all()
        for row in rows:
            owner_id = str(row["intent_id"] or row["reason_run_id"] or "")
            lease_id = str(row["lease_id"] or "")
            task_id = str(row["task_id"])
            if not owner_id or not lease_id or (task_id, owner_id, lease_id) in seen_leases:
                continue
            owner = self._owner_metadata(task_id, owner_id, lease_id)
            source = TokenUsageSourceInput(
                session_file="sessions/missing.jsonl",
                status="incomplete",
                error="run_directory_or_session_missing",
            )
            report.missing_lease_sources += 1
            report.incomplete_sources += 1
            if not dry_run:
                self._persist(owner, [], [source], report)

    def _import_legacy_audit(
        self,
        selected: set[str],
        report: BackfillReport,
        *,
        dry_run: bool,
        session_usage_leases: set[tuple[str, str, str, str]],
    ) -> None:
        query = select(audit_events).where(audit_events.c.payload_json.like('%"token_usage"%'))
        if selected:
            query = query.where(audit_events.c.task_id.in_(selected))
        with self.engine.connect() as conn:
            rows = conn.execute(query).mappings().all()
        for row in rows:
            task_id = str(row["task_id"] or "")
            if not task_id or not self._task_exists(task_id):
                continue
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            usage = payload.get("token_usage")
            if not isinstance(usage, dict):
                continue
            intent_id = (
                str(row["intent_id"] or payload.get("intent_id") or "").strip() or None
            )
            reason_run_id = (
                str(row["reason_run_id"] or payload.get("reason_run_id") or "").strip()
                or None
            )
            normalized = normalize_usage(usage)
            if normalized is None:
                continue
            event_id = str(row["id"])
            lease_id = str(payload.get("lease_id") or f"legacy_{event_id}")
            owner_key = (
                task_id,
                intent_id or "",
                reason_run_id or "",
                lease_id,
            )
            # Only suppress a legacy event when it identifies the same Lease
            # and that Lease already has Session usage.  Older Audit Events
            # predate lease IDs; dropping all of them merely because the Task
            # later acquired a Session would lose valid historical usage.
            same_lease_has_session = bool(payload.get("lease_id")) and self._has_session_usage(
                task_id,
                intent_id,
                reason_run_id,
                lease_id=lease_id,
            )
            if owner_key in session_usage_leases or same_lease_has_session:
                continue
            phase = str(payload.get("phase") or "unknown")
            provider = str(usage.get("provider") or payload.get("provider") or "")
            model = str(usage.get("model") or payload.get("model") or "")
            record = TokenUsageRecordInput(
                session_id="legacy",
                session_file=f"legacy/audit/{event_id}.jsonl",
                session_entry_id=event_id,
                usage_kind="legacy_audit",
                occurred_at=str(row["created_at"]),
                provider=provider,
                model=model,
                requested_model=model,
                **normalized,
            )
            source_record = TokenUsageSourceInput(
                session_file=record.session_file,
                session_id="legacy",
                status="incomplete",
                error="legacy_audit_without_session",
            )
            owner = OwnerMetadata(
                task_id=task_id,
                intent_id=intent_id,
                reason_run_id=reason_run_id,
                lease_id=lease_id,
                phase=phase if phase in {"bootstrap", "explore", "reason"} else "unknown",
                worker=str(row["worker"] or ""),
            )
            report.legacy_imported += 1
            report.records_parsed += 1
            report.incomplete_sources += 1
            if not dry_run:
                self._persist(owner, [record], [source_record], report, source="legacy")

    def _owner_metadata(self, task_id: str, owner_id: str, lease_id: str) -> OwnerMetadata:
        with self.engine.connect() as conn:
            context = conn.execute(
                select(context_revisions).where(
                    context_revisions.c.task_id == task_id,
                    context_revisions.c.lease_id == lease_id,
                    (context_revisions.c.intent_id == owner_id)
                    | (context_revisions.c.reason_run_id == owner_id),
                )
            ).mappings().one_or_none()
            intent = conn.execute(
                select(intents.c.kind, intents.c.worker, intents.c.lease_id).where(
                    intents.c.task_id == task_id,
                    intents.c.id == owner_id,
                )
            ).mappings().one_or_none()
            reason = conn.execute(
                select(reason_runs.c.worker, reason_runs.c.lease_id).where(
                    reason_runs.c.task_id == task_id,
                    reason_runs.c.id == owner_id,
                )
            ).mappings().one_or_none()
            workspace_worker = conn.execute(
                select(reason_workspaces.c.worker).where(
                    reason_workspaces.c.task_id == task_id,
                    reason_workspaces.c.reason_run_id == owner_id,
                    reason_workspaces.c.lease_id == lease_id,
                )
            ).scalar_one_or_none()
            audit_worker = self._audit_worker_for_lease(
                conn,
                task_id=task_id,
                owner_id=owner_id,
                lease_id=lease_id,
            )

        if context is not None and context["reason_run_id"]:
            reason_id = str(context["reason_run_id"])
            return OwnerMetadata(
                task_id=task_id,
                intent_id=None,
                reason_run_id=reason_id,
                lease_id=lease_id,
                phase="reason",
                worker=str(
                    workspace_worker
                    or (reason["worker"] if reason and reason["lease_id"] == lease_id else "")
                    or audit_worker
                    or ""
                ),
            )
        if context is not None and context["intent_id"]:
            intent_id = str(context["intent_id"])
            return OwnerMetadata(
                task_id=task_id,
                intent_id=intent_id,
                reason_run_id=None,
                lease_id=lease_id,
                phase=str(context["phase"] or (intent["kind"] if intent else "unknown")),
                worker=str(
                    (intent["worker"] if intent and intent["lease_id"] == lease_id else "")
                    or audit_worker
                    or ""
                ),
            )
        if reason is not None and reason["lease_id"] == lease_id:
            return OwnerMetadata(
                task_id=task_id,
                intent_id=None,
                reason_run_id=owner_id,
                lease_id=lease_id,
                phase="reason",
                worker=str(
                    workspace_worker
                    or (reason["worker"] if reason and reason["lease_id"] == lease_id else "")
                    or audit_worker
                    or ""
                ),
            )
        if intent is not None and intent["lease_id"] == lease_id:
            return OwnerMetadata(
                task_id=task_id,
                intent_id=owner_id,
                reason_run_id=None,
                lease_id=lease_id,
                phase=str(intent["kind"] or "unknown"),
                worker=str(intent["worker"] or audit_worker or ""),
            )
        if reason is not None:
            return OwnerMetadata(
                task_id=task_id,
                intent_id=None,
                reason_run_id=owner_id,
                lease_id=lease_id,
                phase="reason",
                worker=str(audit_worker or reason["worker"] or ""),
                recognized=False,
                resolution_error="reason_lease_not_found_in_context",
            )
        if intent is not None:
            return OwnerMetadata(
                task_id=task_id,
                intent_id=owner_id,
                reason_run_id=None,
                lease_id=lease_id,
                phase=str(intent["kind"] or "unknown"),
                worker=str(audit_worker or intent["worker"] or ""),
                recognized=False,
                resolution_error="intent_lease_not_found_in_context",
            )
        return OwnerMetadata(
            task_id=task_id,
            intent_id=None,
            reason_run_id=None,
            lease_id=lease_id,
            phase="unknown",
            worker="",
            recognized=False,
            resolution_error="session_owner_not_found",
        )

    @staticmethod
    def _audit_worker_for_lease(
        conn,
        *,
        task_id: str,
        owner_id: str,
        lease_id: str,
    ) -> str:
        rows = conn.execute(
            select(
                audit_events.c.intent_id,
                audit_events.c.reason_run_id,
                audit_events.c.worker,
                audit_events.c.payload_json,
            ).where(
                audit_events.c.task_id == task_id,
                audit_events.c.payload_json.like(f"%{lease_id}%"),
            )
        ).mappings()
        for row in rows:
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if str(payload.get("lease_id") or "") != lease_id:
                continue
            event_owner = str(
                row["intent_id"]
                or row["reason_run_id"]
                or payload.get("intent_id")
                or payload.get("reason_run_id")
                or ""
            )
            if event_owner and event_owner != owner_id:
                continue
            worker = str(row["worker"] or payload.get("worker") or "").strip()
            if worker:
                return worker
        return ""

    def _task_exists(self, task_id: str) -> bool:
        with self.engine.connect() as conn:
            return conn.execute(select(tasks.c.id).where(tasks.c.id == task_id)).scalar_one_or_none() is not None

    def _has_session_usage(
        self,
        task_id: str,
        intent_id: str | None,
        reason_run_id: str | None,
        *,
        lease_id: str | None,
    ) -> bool:
        conditions = [llm_token_usage.c.task_id == task_id, llm_token_usage.c.source != "legacy"]
        if intent_id:
            conditions.append(llm_token_usage.c.intent_id == intent_id)
        elif reason_run_id:
            conditions.append(llm_token_usage.c.reason_run_id == reason_run_id)
        if lease_id:
            conditions.append(llm_token_usage.c.lease_id == lease_id)
        with self.engine.connect() as conn:
            count = conn.execute(
                select(func.count()).select_from(llm_token_usage).where(*conditions)
            ).scalar_one()
        return bool(count)


def _owner_lease_key(owner: OwnerMetadata) -> tuple[str, str, str, str]:
    return (
        owner.task_id,
        owner.intent_id or "",
        owner.reason_run_id or "",
        owner.lease_id,
    )


def _source_error(owner: OwnerMetadata, error: str) -> str:
    if owner.recognized:
        return error
    return ";".join(part for part in (owner.resolution_error, error) if part)[:1_000]
