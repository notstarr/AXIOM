from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from axiom.modules.token_usage.backfill import TokenUsageBackfiller
from axiom.modules.token_usage.parser import parse_session_file
from axiom.modules.token_usage.schemas import (
    TokenUsageRecordInput,
    TokenUsageSourceInput,
)
from axiom.modules.token_usage.service import TokenUsageService
from axiom.modules.token_usage.service import range_start
from axiom.modules.token_usage.tables import llm_token_usage_sources
from axiom.modules.reason.tables import reason_runs
from axiom.modules.audit.tables import audit_events
from axiom.shared.clock import utcnow


def _session_event(event_id: str, usage: dict, *, role: str = "assistant") -> dict:
    return {
        "type": "message",
        "id": event_id,
        "timestamp": "2026-08-03T01:02:03.000Z",
        "message": {
            "role": role,
            "provider": "axiom",
            "model": "requested-model",
            "responseModel": "actual-model",
            "usage": usage,
        },
    }


def test_session_parser_extracts_all_explicit_usage_kinds_without_retained_tail(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    events = [
        {"type": "session", "id": "session-1"},
        {"type": "model_change", "provider": "axiom", "modelId": "requested-model"},
        _session_event(
            "assistant-1",
            {
                "input": 10,
                "cacheRead": 20,
                "cacheWrite": 3,
                "output": 4,
                "reasoning": 2,
                "totalTokens": 37,
            },
        ),
        _session_event("tool-1", {"input": 1, "output": 2}, role="toolResult"),
        {
            "type": "compaction",
            "id": "compaction-1",
            "timestamp": "2026-08-03T01:03:03.000Z",
            "compaction": {"usage": {"input": 5, "cacheRead": 6, "output": 7}},
        },
        {
            "type": "branch_summary",
            "id": "branch-1",
            "timestamp": "2026-08-03T01:04:03.000Z",
            "summary": {"usage": {"input": 8, "cacheWrite": 1, "output": 2}},
        },
        {
            "type": "message",
            "id": "retained-tail-only",
            "timestamp": "2026-08-03T01:05:03.000Z",
            "message": {
                "role": "user",
                "retainedTail": {"usage": {"input": 999}},
            },
        },
    ]
    path.write_text("\n".join(json.dumps(item) for item in events) + "\n", encoding="utf-8")

    parsed = parse_session_file(path, session_file="runs/t_001/i001/lease/sessions/session.jsonl")

    assert [record.usage_kind for record in parsed.records] == [
        "assistant",
        "tool_result",
        "compaction",
        "branch_summary",
    ]
    assistant = parsed.records[0]
    assert assistant.model == "actual-model"
    assert assistant.requested_model == "requested-model"
    assert assistant.reasoning_tokens == 2
    assert assistant.reported_total_tokens == 37
    assert parsed.source is not None
    assert parsed.source.status == "complete"


def test_session_parser_marks_corrupt_or_missing_session_sources_incomplete(tmp_path: Path) -> None:
    path = tmp_path / "broken.jsonl"
    path.write_text(
        json.dumps(_session_event("e1", {"input": 1, "output": 1})) + "\nnot-json",
        encoding="utf-8",
    )

    parsed = parse_session_file(path)

    assert parsed.records
    assert parsed.source is not None
    assert parsed.source.status == "incomplete"
    assert "invalid_json_line" in parsed.source.error
    assert "missing_session_id" in parsed.source.error


def _record(
    entry_id: str,
    *,
    model: str = "model-a",
    occurred_at: str | None = None,
) -> TokenUsageRecordInput:
    return TokenUsageRecordInput(
        session_file="sessions/session.jsonl",
        session_id="session-1",
        session_entry_id=entry_id,
        usage_kind="assistant",
        occurred_at=occurred_at or utcnow(),
        provider="provider-a",
        model=model,
        requested_model=model,
        input_tokens=10,
        cache_read_tokens=20,
        cache_write_tokens=2,
        output_tokens=8,
        reasoning_tokens=3,
    )


def _source(*, status: str = "complete", name: str = "sessions/session.jsonl") -> TokenUsageSourceInput:
    return TokenUsageSourceInput(
        session_file=name,
        session_id="session-1",
        status=status,  # type: ignore[arg-type]
        error="missing session" if status == "incomplete" else "",
    )


def test_token_service_is_idempotent_and_exposes_four_categories(client, engine) -> None:
    project = client.post("/api/v1/projects", json={"title": "token"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={"project_id": project["id"], "title": "token task", "origin": "example.com", "goal": "g"},
    ).json()
    service = TokenUsageService(engine)

    first = service.record_backfill_batch(
        task_id=task["id"],
        intent_id=None,
        reason_run_id=None,
        lease_id="lease-1",
        phase="explore",
        worker="worker-a",
        records=[_record("entry-1")],
        sources=[_source()],
    )
    duplicate = service.record_backfill_batch(
        task_id=task["id"],
        intent_id=None,
        reason_run_id=None,
        lease_id="lease-1",
        phase="explore",
        worker="worker-a",
        records=[_record("entry-1")],
        sources=[_source()],
    )

    assert first.inserted == 1
    assert duplicate.inserted == 0
    assert duplicate.duplicate == 1
    summary = client.get("/api/v1/dashboard/summary?token_range=7d").json()
    usage = summary["token_usage"]
    assert usage["input"] == 10
    assert usage["input_uncached"] == 10
    assert usage["cache_read"] == 20
    assert usage["cache_write"] == 2
    assert usage["output"] == 8
    assert usage["total"] == 40
    assert usage["reasoning"] == 3
    assert usage["cache_hit_rate"] == 20 / 32
    assert len(summary["token_series"]) == 7
    assert summary["token_series"][-1]["total"] == 40


def test_token_identity_deduplicates_live_and_backfill_owner_resolution(client, engine) -> None:
    project = client.post("/api/v1/projects", json={"title": "token identity"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={"project_id": project["id"], "title": "token task", "origin": "example.com", "goal": "g"},
    ).json()
    service = TokenUsageService(engine)
    record = _record("same-entry")

    first = service.record_backfill_batch(
        task_id=task["id"],
        intent_id="i001",
        reason_run_id=None,
        lease_id="lease-live",
        phase="explore",
        worker="worker-a",
        records=[record],
        sources=[],
    )
    replay = service.record_backfill_batch(
        task_id=task["id"],
        intent_id=None,
        reason_run_id="rr001",
        lease_id="lease-backfill",
        phase="reason",
        worker="worker-b",
        records=[record],
        sources=[],
    )

    assert first.inserted == 1
    assert replay.inserted == 0
    assert replay.duplicate == 1


def test_internal_intent_and_reason_endpoints_accept_only_active_matching_leases(client, engine) -> None:
    worker = client.put(
        "/api/v1/workers/token-worker",
        json={"config": {"model": "model-a"}},
    )
    assert worker.status_code == 200, worker.text
    project = client.post("/api/v1/projects", json={"title": "live usage"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "live usage task",
            "origin": "example.com",
            "goal": "g",
            "worker_pool": ["token-worker"],
        },
    ).json()
    claimed = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/intents/i001/claim",
        json={"worker_name": "token-worker"},
    ).json()
    lease_id = claimed["lease_id"]
    assert client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/intents/i001/start",
        json={"lease_id": lease_id},
    ).status_code == 200
    command = {
        "lease_id": lease_id,
        "records": [_record("live-entry").model_dump(mode="json")],
        "sources": [_source().model_dump(mode="json")],
    }

    accepted = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/intents/i001/token-usage",
        json=command,
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["inserted"] == 1
    rejected = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/intents/i001/token-usage",
        json={**command, "lease_id": "wrong-lease"},
    )
    assert rejected.status_code == 409

    now = utcnow()
    with engine.begin() as conn:
        conn.execute(
            reason_runs.insert().values(
                id="rr-token",
                task_id=task["id"],
                goal_cycle_id=task.get("current_goal_cycle_id"),
                status="running",
                trigger="manual",
                trigger_payload_json="{}",
                input_fingerprint="token-fixture",
                worker_pool_json='["token-worker"]',
                worker="token-worker",
                settings_version=1,
                execute_timeout_seconds=600,
                conclude_timeout_seconds=180,
                target_open_intents=4,
                max_new_intents=8,
                explore_execute_min_seconds=60,
                explore_execute_max_seconds=3600,
                explore_conclude_min_seconds=180,
                explore_conclude_max_seconds=600,
                lease_id="reason-lease",
                created_at=now,
                updated_at=now,
            )
        )
    reason_command = {
        "lease_id": "reason-lease",
        "records": [_record("reason-entry").model_dump(mode="json")],
        "sources": [],
    }
    reason_accepted = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/reason-runs/rr-token/token-usage",
        json=reason_command,
    )
    assert reason_accepted.status_code == 200, reason_accepted.text
    assert reason_accepted.json()["inserted"] == 1
    reason_rejected = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/reason-runs/rr-token/token-usage",
        json={**reason_command, "lease_id": "wrong-lease"},
    )
    assert reason_rejected.status_code == 409


def test_dashboard_incomplete_count_is_per_task_and_model_filter_works(client, engine) -> None:
    project = client.post("/api/v1/projects", json={"title": "token"}).json()
    first_task = client.post(
        "/api/v1/tasks",
        json={"project_id": project["id"], "title": "first", "origin": "one.example.com", "goal": "g"},
    ).json()
    second_task = client.post(
        "/api/v1/tasks",
        json={"project_id": project["id"], "title": "second", "origin": "two.example.com", "goal": "g"},
    ).json()
    service = TokenUsageService(engine)
    service.record_backfill_batch(
        task_id=first_task["id"],
        intent_id=None,
        reason_run_id=None,
        lease_id="lease-1",
        phase="reason",
        worker="reason-a",
        records=[_record("first", model="model-a")],
        sources=[_source()],
    )
    service.record_backfill_batch(
        task_id=second_task["id"],
        intent_id=None,
        reason_run_id=None,
        lease_id="lease-2",
        phase="reason",
        worker="reason-b",
        records=[],
        sources=[_source(status="incomplete", name="sessions/missing.jsonl")],
    )

    summary = client.get("/api/v1/dashboard/summary?token_range=7d&model=provider-a/model-a").json()
    by_task = {item["task_id"]: item for item in summary["token_by_task"]}
    assert by_task[first_task["id"]]["incomplete_sources"] == 0
    assert by_task[second_task["id"]]["incomplete_sources"] == 1
    assert by_task[second_task["id"]]["total"] == 0
    assert summary["token_usage"]["incomplete_sources"] == 1


def test_backfill_dry_run_remembers_session_owners_before_legacy_import(client, engine, data_dir) -> None:
    project = client.post("/api/v1/projects", json={"title": "backfill"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={"project_id": project["id"], "title": "backfill task", "origin": "example.com", "goal": "g"},
    ).json()
    run_dir = data_dir / "runs" / task["id"] / "i001" / "lease-1" / "sessions"
    run_dir.mkdir(parents=True)
    events = [{"type": "session", "id": "session-1"}, _session_event("e1", {"input": 3, "output": 4})]
    (run_dir / "session.jsonl").write_text(
        "\n".join(json.dumps(item) for item in events) + "\n", encoding="utf-8"
    )
    with engine.begin() as conn:
        conn.execute(
            audit_events.insert().values(
                id="evt-same-lease",
                project_id=project["id"],
                task_id=task["id"],
                intent_id="i001",
                reason_run_id=None,
                workspace_id=None,
                worker="worker-a",
                event_type="bootstrap.completed",
                level="info",
                message="legacy usage",
                payload_json=json.dumps(
                    {
                        "lease_id": "lease-1",
                        "phase": "bootstrap",
                        "token_usage": {"input_tokens": 3, "output_tokens": 4},
                    }
                ),
                created_at=utcnow(),
            )
        )

    backfiller = TokenUsageBackfiller(engine, data_dir)
    owner = backfiller._owner_metadata(task["id"], "i001", "lease-1")
    report = backfiller.run(dry_run=True)

    assert owner.worker == "worker-a"
    assert report.records_parsed == 1
    assert report.legacy_imported == 0


def test_backfill_keeps_unleased_legacy_usage_when_task_has_newer_sessions(client, engine, data_dir) -> None:
    project = client.post("/api/v1/projects", json={"title": "mixed history"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={"project_id": project["id"], "title": "mixed task", "origin": "example.com", "goal": "g"},
    ).json()
    run_dir = data_dir / "runs" / task["id"] / "i001" / "lease-new" / "sessions"
    run_dir.mkdir(parents=True)
    (run_dir / "session.jsonl").write_text(
        "\n".join(
            json.dumps(item)
            for item in [
                {"type": "session", "id": "session-new"},
                _session_event("new-entry", {"input": 3, "output": 4}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    with engine.begin() as conn:
        conn.execute(
            audit_events.insert().values(
                id="evt-old-unleased",
                project_id=project["id"],
                task_id=task["id"],
                intent_id=None,
                reason_run_id=None,
                workspace_id=None,
                worker="legacy-worker",
                event_type="explore.completed",
                level="info",
                message="old aggregate usage",
                payload_json=json.dumps(
                    {"token_usage": {"input_tokens": 11, "output_tokens": 5}}
                ),
                created_at=utcnow(),
            )
        )

    report = TokenUsageBackfiller(engine, data_dir).run(dry_run=True)

    assert report.records_parsed == 2
    assert report.legacy_imported == 1


def test_backfill_ignores_non_lease_working_directories(client, engine, data_dir) -> None:
    project = client.post("/api/v1/projects", json={"title": "run layout"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "run layout task",
            "origin": "example.com",
            "goal": "g",
        },
    ).json()
    task_run_dir = data_dir / "runs" / task["id"]
    (task_run_dir / "artifacts" / "evidence").mkdir(parents=True)
    (task_run_dir / "i001" / "private").mkdir(parents=True)

    report = TokenUsageBackfiller(engine, data_dir).run(
        task_ids={task["id"]},
        dry_run=True,
    )

    assert report.lease_directories == 0
    assert report.session_files == 0
    assert report.incomplete_sources == 0
    assert report.errors == []


def test_time_ranges_align_totals_with_displayed_calendar_buckets() -> None:
    now = datetime(2026, 8, 3, 15, 30, tzinfo=timezone.utc)

    assert range_start("7d", now=now) == "2026-07-28T00:00:00Z"
    assert range_start("30d", now=now) == "2026-07-05T00:00:00Z"
    # 90 days before August 3 is May 5; the chart starts at that ISO week.
    assert range_start("3m", now=now) == "2026-05-04T00:00:00Z"
    assert range_start("1y", now=now) == "2025-09-01T00:00:00Z"


def test_real_session_source_replaces_provisional_missing_marker(client, engine) -> None:
    project = client.post("/api/v1/projects", json={"title": "source recovery"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={"project_id": project["id"], "title": "task", "origin": "example.com", "goal": "g"},
    ).json()
    service = TokenUsageService(engine)

    service.record_backfill_batch(
        task_id=task["id"],
        intent_id="i001",
        reason_run_id=None,
        lease_id="lease-1",
        phase="explore",
        worker="worker-a",
        records=[],
        sources=[_source(status="incomplete", name="sessions/missing.jsonl")],
    )
    service.record_backfill_batch(
        task_id=task["id"],
        intent_id="i001",
        reason_run_id=None,
        lease_id="lease-1",
        phase="explore",
        worker="worker-a",
        records=[_record("entry-1")],
        sources=[_source(name="sessions/actual.jsonl")],
    )

    with engine.connect() as conn:
        sources = conn.execute(
            llm_token_usage_sources.select().where(
                llm_token_usage_sources.c.task_id == task["id"]
            )
        ).mappings().all()
    assert [source["session_file"] for source in sources] == ["sessions/actual.jsonl"]


def test_tracker_reports_usage_and_marks_missing_session(tmp_path: Path) -> None:
    from axiom.dispatcher.activity import SessionActivityTracker

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    session = session_dir / "live.jsonl"
    session.write_text(
        json.dumps({"type": "session", "id": "session-1"}) + "\n",
        encoding="utf-8",
    )
    commands = []
    tracker = SessionActivityTracker(
        session_dir,
        "lease-1",
        None,
        usage_reporter=commands.append,
        poll_interval_seconds=1,
    )
    tracker.start()
    session.write_text(
        session.read_text(encoding="utf-8")
        + json.dumps(_session_event("assistant-1", {"input": 2, "output": 3}))
        + "\n",
        encoding="utf-8",
    )
    tracker.stop()

    records = [record for command in commands for record in command.records]
    sources = [source for command in commands for source in command.sources]
    assert any(record.session_entry_id == "assistant-1" for record in records)
    assert any(source.session_file == "sessions/live.jsonl" and source.status == "complete" for source in sources)

    missing_commands = []
    missing_tracker = SessionActivityTracker(
        tmp_path / "empty-sessions",
        "lease-2",
        None,
        usage_reporter=missing_commands.append,
        poll_interval_seconds=1,
    )
    missing_tracker.start()
    missing_tracker.stop()
    missing_sources = [source for command in missing_commands for source in command.sources]
    assert any(source.session_file == "sessions/missing.jsonl" for source in missing_sources)
