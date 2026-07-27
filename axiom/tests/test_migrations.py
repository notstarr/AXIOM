from __future__ import annotations

import json

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import inspect, select

from axiom.cli import PROJECT_ROOT, alembic_config, database_heads, migration_heads
from axiom.modules.audit.tables import audit_events
from axiom.modules.context.tables import context_revisions
from axiom.modules.goals.tables import goal_evaluations
from axiom.modules.graph.tables import facts, intent_sources, intents
from axiom.modules.projects.tables import projects
from axiom.modules.reason.tables import reason_run_sources, reason_runs, reason_workspaces
from axiom.modules.settings.tables import runtime_settings
from axiom.modules.tasks.tables import tasks
from axiom.shared.ids import id_counters
from axiom.shared import model_registry as _model_registry  # noqa: F401
from axiom.shared.database import create_sqlite_engine, metadata, schema_is_ready


def test_migrations_reach_head_without_metadata_drift(tmp_path) -> None:
    database_path = tmp_path / "migrated.db"
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{database_path}")

    command.upgrade(config, "head")

    engine = create_sqlite_engine(database_path)
    try:
        assert schema_is_ready(engine)
        assert database_heads(engine) == migration_heads(alembic_config(database_path))
        with engine.connect() as conn:
            context = MigrationContext.configure(
                conn,
                opts={"compare_type": True, "render_as_batch": True},
            )
            assert compare_metadata(context, metadata) == []
    finally:
        engine.dispose()


def test_credential_text_migration_roundtrip_preserves_legacy_material(tmp_path) -> None:
    database_path = tmp_path / "credential-roundtrip.db"
    config = alembic_config(database_path)
    command.upgrade(config, "20260727_0020")

    engine = create_sqlite_engine(database_path)
    now = "2026-07-27T00:00:00Z"
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO projects "
                "(id, title, description, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("prj_001", "credentials", "", "active", now, now),
            )
            conn.exec_driver_sql(
                "INSERT INTO credentials "
                "(id, project_id, name, credential_type, target_host, data_json, "
                "enabled, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "cred_001",
                    "prj_001",
                    "legacy-json",
                    "headers",
                    "a.example.com",
                    json.dumps(
                        {
                            "Cookie": "session=test-secret",
                            "_legacy_notes": "旧备注",
                            "_legacy_domains": ["b.example.com", "a.example.com"],
                        },
                        ensure_ascii=False,
                    ),
                    True,
                    now,
                    now,
                ),
            )
            conn.exec_driver_sql(
                "INSERT INTO credentials "
                "(id, project_id, name, credential_type, target_host, data_json, "
                "enabled, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "cred_002",
                    "prj_001",
                    "legacy-text",
                    "ssh",
                    "",
                    "not-json: {broken",
                    False,
                    now,
                    now,
                ),
            )
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    upgraded = create_sqlite_engine(database_path)
    try:
        with upgraded.begin() as conn:
            columns = {item["name"] for item in inspect(conn).get_columns("credentials")}
            assert {"content", "notes"}.issubset(columns)
            assert {"credential_type", "target_host", "data_json"}.isdisjoint(columns)
            first = conn.exec_driver_sql(
                "SELECT content, notes FROM credentials WHERE id = 'cred_001'"
            ).mappings().one()
            assert json.loads(first["content"]) == {"Cookie": "session=test-secret"}
            assert "旧备注" in first["notes"]
            assert "认证类型：headers" in first["notes"]
            assert "适用域名：a.example.com、b.example.com" in first["notes"]
            assert conn.exec_driver_sql(
                "SELECT content FROM credentials WHERE id = 'cred_002'"
            ).scalar_one() == "not-json: {broken"
            assert conn.exec_driver_sql(
                "SELECT COUNT(*) FROM credential_refreshes"
            ).scalar_one() == 0
            conn.exec_driver_sql(
                "INSERT INTO credential_refreshes "
                "(id, credential_id, content, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("crf_001", "cred_001", "重新登录并读取 Set-Cookie", now, now),
            )
    finally:
        upgraded.dispose()

    command.downgrade(config, "20260727_0020")
    downgraded = create_sqlite_engine(database_path)
    try:
        with downgraded.connect() as conn:
            assert "credential_refreshes" not in inspect(conn).get_table_names()
            restored = json.loads(
                conn.exec_driver_sql(
                    "SELECT data_json FROM credentials WHERE id = 'cred_001'"
                ).scalar_one()
            )
            assert restored["Cookie"] == "session=test-secret"
            assert restored["_legacy_notes"] == "旧备注"
            assert restored["_legacy_domains"] == ["a.example.com", "b.example.com"]
            assert restored["_legacy_refresh"] == "重新登录并读取 Set-Cookie"
            assert conn.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
            assert conn.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        downgraded.dispose()

    command.upgrade(config, "head")
    reupgraded = create_sqlite_engine(database_path)
    try:
        with reupgraded.connect() as conn:
            refresh = conn.exec_driver_sql(
                "SELECT credential_id, content FROM credential_refreshes"
            ).mappings().one()
            assert refresh == {
                "credential_id": "cred_001",
                "content": "重新登录并读取 Set-Cookie",
            }
            content = conn.exec_driver_sql(
                "SELECT content FROM credentials WHERE id = 'cred_001'"
            ).scalar_one()
            assert "_legacy_refresh" not in content
            assert conn.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
            assert conn.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        reupgraded.dispose()


def test_vulnerability_artifact_migration_preserves_v1_rows_and_downgrades(tmp_path) -> None:
    database_path = tmp_path / "vulnerability-artifacts-roundtrip.db"
    config = alembic_config(database_path)
    command.upgrade(config, "20260727_0021")
    now = "2026-07-27T00:00:00Z"
    engine = create_sqlite_engine(database_path)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO projects "
                "(id, title, description, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("prj_001", "migration", "", "active", now, now),
            )
            conn.exec_driver_sql(
                "INSERT INTO tasks "
                "(id, project_id, title, origin_host, origin_scheme, origin_port, "
                "origin_input, goal, status, phase, scope_json, worker_pool_json, "
                "guard_enabled, external_report_enabled, legacy_readonly, created_at, "
                "updated_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "t_001",
                    "prj_001",
                    "migration",
                    "example.com",
                    "https",
                    "443",
                    "https://example.com",
                    "goal",
                    "completed",
                    "none",
                    "{}",
                    "[]",
                    False,
                    False,
                    False,
                    now,
                    now,
                    now,
                ),
            )
            conn.exec_driver_sql(
                "INSERT INTO artifacts "
                "(id, task_id, intent_id, artifact_type, storage_key, summary, "
                "content_hash, size, created_by, created_at, archived) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "a001",
                    "t_001",
                    None,
                    "upload",
                    "tasks/t_001/uploads/legacy/findings.md",
                    "legacy",
                    "0" * 64,
                    10,
                    "human",
                    now,
                    False,
                ),
            )
            conn.exec_driver_sql(
                "INSERT INTO vulnerabilities "
                "(id, task_id, intent_id, title, type, severity, description, "
                "prerequisites, impact, reproduction_steps_json, remediation, "
                "curl_command, evidence_json, affected_target, status, status_reason, "
                "source, reported_by, created_at, updated_at, archived) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "v001",
                    "t_001",
                    None,
                    "legacy",
                    "idor",
                    "high",
                    "description",
                    "",
                    "impact",
                    '["legacy step"]',
                    "fix",
                    "curl legacy",
                    '["legacy evidence"]',
                    "example.com",
                    "待验证",
                    "",
                    "legacy",
                    "migration",
                    now,
                    now,
                    False,
                ),
            )
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    upgraded = create_sqlite_engine(database_path)
    try:
        with upgraded.begin() as conn:
            assert conn.exec_driver_sql(
                "SELECT format_version FROM vulnerabilities WHERE id = 'v001'"
            ).scalar_one() == 1
            conn.exec_driver_sql(
                "INSERT INTO vulnerability_artifacts "
                "(task_id, vulnerability_id, artifact_id, role, display_order) "
                "VALUES (?, ?, ?, ?, ?)",
                ("t_001", "v001", "a001", "finding", 0),
            )
            assert conn.exec_driver_sql(
                "SELECT COUNT(*) FROM vulnerability_artifacts"
            ).scalar_one() == 1
    finally:
        upgraded.dispose()

    command.downgrade(config, "20260727_0021")
    downgraded = create_sqlite_engine(database_path)
    try:
        with downgraded.connect() as conn:
            tables = set(inspect(conn).get_table_names())
            columns = {item["name"] for item in inspect(conn).get_columns("vulnerabilities")}
            assert "vulnerability_artifacts" not in tables
            assert "format_version" not in columns
            assert conn.exec_driver_sql(
                "SELECT reproduction_steps_json FROM vulnerabilities WHERE id = 'v001'"
            ).scalar_one() == '["legacy step"]'
            assert conn.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
            assert conn.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        downgraded.dispose()


def test_reason_run_migration_can_downgrade_after_real_runs(tmp_path) -> None:
    database_path = tmp_path / "reason-run-roundtrip.db"
    config = alembic_config(database_path)
    command.upgrade(config, "head")

    engine = create_sqlite_engine(database_path)
    now = "2026-07-27T00:00:00Z"
    lease_id = "lease_roundtrip"
    try:
        with engine.begin() as conn:
            conn.execute(
                projects.insert().values(
                    id="prj_001",
                    title="migration",
                    description="",
                    status="active",
                    created_at=now,
                    updated_at=now,
                )
            )
            conn.execute(
                tasks.insert().values(
                    id="t_001",
                    project_id="prj_001",
                    title="migration",
                    origin_host="example.com",
                    origin_scheme="https",
                    origin_port="443",
                    origin_input="https://example.com",
                    goal="goal",
                    status="completed",
                    phase="none",
                    scope_json="{}",
                    worker_pool_json="[]",
                    guard_enabled=False,
                    external_report_enabled=False,
                    legacy_readonly=False,
                    created_at=now,
                    updated_at=now,
                    completed_at=now,
                )
            )
            conn.execute(
                facts.insert(),
                [
                    {
                        "id": "origin",
                        "task_id": "t_001",
                        "description": "origin",
                        "created_at": now,
                        "archived": False,
                    },
                    {
                        "id": "goal",
                        "task_id": "t_001",
                        "description": "goal",
                        "created_at": now,
                        "archived": False,
                    },
                    {
                        "id": "f001",
                        "task_id": "t_001",
                        "description": "evidence",
                        "created_at": now,
                        "archived": False,
                    },
                ],
            )
            conn.execute(
                intents.insert().values(
                    id="i001",
                    task_id="t_001",
                    kind="bootstrap",
                    description="bootstrap",
                    status="completed",
                    creator="test",
                    worker_pool_json="[]",
                    targets_json="[]",
                    retry_count=0,
                    infrastructure_retry_count=0,
                    last_failure_kind="",
                    failure_class="",
                    activity_kind="completed",
                    activity_summary="",
                    activity_tool="",
                    created_at=now,
                    concluded_at=now,
                    archived=False,
                )
            )
            conn.execute(
                id_counters.insert().values(
                    scope="t_001",
                    kind="intent",
                    next_value=2,
                )
            )
            conn.execute(
                reason_runs.insert().values(
                    id="rr000001",
                    task_id="t_001",
                    status="completed",
                    trigger="initial",
                    trigger_payload_json="{}",
                    input_fingerprint="fingerprint",
                    worker_pool_json="[]",
                    worker="worker-1",
                    settings_version=2,
                    execute_timeout_seconds=300,
                    conclude_timeout_seconds=120,
                    target_open_intents=4,
                    max_new_intents=4,
                    explore_execute_min_seconds=60,
                    explore_execute_max_seconds=3600,
                    explore_conclude_min_seconds=30,
                    explore_conclude_max_seconds=600,
                    lease_id=lease_id,
                    claimed_at=now,
                    last_heartbeat_at=now,
                    retry_count=0,
                    infrastructure_retry_count=0,
                    last_failure_kind="",
                    failure_class="",
                    activity_kind="completed",
                    activity_summary="done",
                    activity_tool="",
                    activity_started_at=now,
                    activity_updated_at=now,
                    created_at=now,
                    updated_at=now,
                    concluded_at=now,
                )
            )
            conn.execute(
                reason_run_sources.insert().values(
                    task_id="t_001",
                    reason_run_id="rr000001",
                    fact_id="f001",
                )
            )
            conn.execute(
                reason_workspaces.insert().values(
                    id="rw000001",
                    task_id="t_001",
                    intent_id=None,
                    reason_run_id="rr000001",
                    lease_id=lease_id,
                    status="committed",
                    context_revision=1,
                    draft_json="{}",
                    worker="worker-1",
                    failure_reason="",
                    result_json="{}",
                    created_at=now,
                    updated_at=now,
                    committed_at=now,
                )
            )
            conn.execute(
                context_revisions.insert().values(
                    task_id="t_001",
                    revision=1,
                    phase="reason",
                    intent_id=None,
                    reason_run_id="rr000001",
                    lease_id=lease_id,
                    manifest_key="contexts/t_001/r000001/manifest.json",
                    content_hash="hash",
                    created_at=now,
                )
            )
            conn.execute(
                goal_evaluations.insert().values(
                    id="ge001",
                    task_id="t_001",
                    intent_id=None,
                    reason_run_id="rr000001",
                    workspace_id="rw000001",
                    achieved=True,
                    reason="done",
                    evidence_refs_json='["fact:f001"]',
                    evaluator="worker-1",
                    created_at=now,
                )
            )
            conn.execute(
                audit_events.insert().values(
                    id="evt_001",
                    project_id="prj_001",
                    task_id="t_001",
                    intent_id=None,
                    reason_run_id="rr000001",
                    workspace_id="rw000001",
                    worker="worker-1",
                    event_type="reason.committed",
                    level="info",
                    message="done",
                    payload_json="{}",
                    created_at=now,
                )
            )
            conn.execute(
                runtime_settings.insert().values(
                    id="runtime",
                    config_json=json.dumps(
                        {
                            "global_max_active_runs": 7,
                            "global_max_active_tasks": 3,
                            "reason_conclude_seconds": 120,
                        }
                    ),
                    version=2,
                    updated_by="test",
                    updated_at=now,
                    applied_version=2,
                    applied_at=now,
                )
            )
    finally:
        engine.dispose()

    command.downgrade(config, "20260724_0019")
    downgraded = create_sqlite_engine(database_path)
    try:
        with downgraded.connect() as conn:
            assert "reason_runs" not in inspect(conn).get_table_names()
            legacy = conn.execute(
                select(intents).where(
                    intents.c.task_id == "t_001",
                    intents.c.kind == "reason",
                )
            ).mappings().one()
            assert legacy["id"] == "i002"
            assert legacy["to_fact_id"] == "goal"
            assert conn.execute(
                select(intent_sources.c.fact_id).where(
                    intent_sources.c.task_id == "t_001",
                    intent_sources.c.intent_id == "i002",
                )
            ).scalar_one() == "f001"
            workspace = conn.exec_driver_sql(
                "SELECT intent_id FROM reason_workspaces WHERE id = 'rw000001'"
            ).scalar_one()
            assert workspace == "i002"
            context_intent = conn.exec_driver_sql(
                "SELECT intent_id FROM context_revisions WHERE task_id = 't_001'"
            ).scalar_one()
            assert context_intent == "i002"
            goal_intent = conn.exec_driver_sql(
                "SELECT intent_id FROM goal_evaluations WHERE id = 'ge001'"
            ).scalar_one()
            assert goal_intent == "i002"
            audit_intent = conn.exec_driver_sql(
                "SELECT intent_id FROM audit_events WHERE id = 'evt_001'"
            ).scalar_one()
            assert audit_intent == "i002"
            runtime = json.loads(
                conn.exec_driver_sql(
                    "SELECT config_json FROM runtime_settings WHERE id = 'runtime'"
                ).scalar_one()
            )
            assert runtime["max_parallel"] == 7
            assert "global_max_active_runs" not in runtime
            assert conn.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
            assert conn.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        downgraded.dispose()

    command.upgrade(config, "head")
    upgraded = create_sqlite_engine(database_path)
    try:
        with upgraded.connect() as conn:
            assert database_heads(upgraded) == migration_heads(config)
            assert conn.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
            assert conn.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            legacy = conn.execute(
                select(intents.c.id).where(
                    intents.c.task_id == "t_001",
                    intents.c.kind == "reason",
                )
            ).scalar_one()
            assert legacy == "i002"
    finally:
        upgraded.dispose()
