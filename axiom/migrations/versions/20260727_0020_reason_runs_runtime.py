"""Separate ReasonRun execution from Intents and formalize runtime settings."""

from __future__ import annotations

import json

from alembic import op
import sqlalchemy as sa


revision = "20260727_0020"
down_revision = "20260724_0019"
branch_labels = None
depends_on = None


ACTIVE_REASON_SQL = "status IN ('pending','claimed','running','concluding')"


def upgrade() -> None:
    op.create_table(
        "reason_runs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("trigger", sa.String(), nullable=False),
        sa.Column("trigger_payload_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("input_fingerprint", sa.String(), nullable=False),
        sa.Column("worker_pool_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("worker", sa.String(), nullable=True),
        sa.Column("settings_version", sa.Integer(), nullable=False),
        sa.Column("execute_timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("conclude_timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("target_open_intents", sa.Integer(), nullable=False),
        sa.Column("max_new_intents", sa.Integer(), nullable=False),
        sa.Column("explore_execute_min_seconds", sa.Integer(), nullable=False),
        sa.Column("explore_execute_max_seconds", sa.Integer(), nullable=False),
        sa.Column("explore_conclude_min_seconds", sa.Integer(), nullable=False),
        sa.Column("explore_conclude_max_seconds", sa.Integer(), nullable=False),
        sa.Column("lease_id", sa.String(), nullable=True),
        sa.Column("claimed_at", sa.String(), nullable=True),
        sa.Column("last_heartbeat_at", sa.String(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "infrastructure_retry_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("next_retry_at", sa.String(), nullable=True),
        sa.Column("last_failure_kind", sa.String(), nullable=False, server_default=""),
        sa.Column("failure_class", sa.String(), nullable=False, server_default=""),
        sa.Column("activity_kind", sa.String(), nullable=False, server_default=""),
        sa.Column("activity_summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("activity_tool", sa.String(), nullable=False, server_default=""),
        sa.Column("activity_started_at", sa.String(), nullable=True),
        sa.Column("activity_updated_at", sa.String(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.Column("concluded_at", sa.String(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending','claimed','running','concluding','completed','failed','cancelled')",
            name="ck_reason_runs_status",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            name="fk_reason_runs_task_id_tasks",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_reason_runs"),
        sa.UniqueConstraint("task_id", "id", name="uq_reason_runs_task_id_id"),
    )
    op.create_index(
        "ix_reason_runs_task_status",
        "reason_runs",
        ["task_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_reason_runs_retry",
        "reason_runs",
        ["status", "next_retry_at"],
        unique=False,
    )
    op.create_index(
        "uq_reason_runs_one_open_per_task",
        "reason_runs",
        ["task_id"],
        unique=True,
        sqlite_where=sa.text(ACTIVE_REASON_SQL),
    )

    op.create_table(
        "reason_run_sources",
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("reason_run_id", sa.String(), nullable=False),
        sa.Column("fact_id", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id", "fact_id"],
            ["facts.task_id", "facts.id"],
            name="fk_reason_run_sources_task_id_fact_id_facts",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["task_id", "reason_run_id"],
            ["reason_runs.task_id", "reason_runs.id"],
            name="fk_reason_run_sources_task_id_reason_run_id_reason_runs",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "reason_run_id",
            "fact_id",
            name="uq_reason_run_sources_reason_run_id_fact_id",
        ),
    )
    op.create_index(
        "ix_reason_run_sources_task_fact",
        "reason_run_sources",
        ["task_id", "fact_id"],
        unique=False,
    )

    with op.batch_alter_table("reason_workspaces") as batch:
        batch.drop_constraint(
            "uq_reason_workspaces_task_id_intent_id_lease_id",
            type_="unique",
        )
        batch.alter_column("intent_id", existing_type=sa.String(), nullable=True)
        batch.add_column(sa.Column("reason_run_id", sa.String(), nullable=True))
        batch.create_foreign_key(
            "fk_reason_workspaces_task_id_reason_run_id_reason_runs",
            "reason_runs",
            ["task_id", "reason_run_id"],
            ["task_id", "id"],
            ondelete="CASCADE",
        )
        batch.create_check_constraint(
            "ck_reason_workspaces_owner",
            "(intent_id IS NOT NULL AND reason_run_id IS NULL) OR "
            "(intent_id IS NULL AND reason_run_id IS NOT NULL)",
        )
        batch.create_unique_constraint(
            "uq_reason_workspaces_task_id_intent_id_lease_id",
            ["task_id", "intent_id", "lease_id"],
        )
        batch.create_unique_constraint(
            "uq_reason_workspaces_task_id_reason_run_id_lease_id",
            ["task_id", "reason_run_id", "lease_id"],
        )

    with op.batch_alter_table("context_revisions") as batch:
        batch.add_column(sa.Column("reason_run_id", sa.String(), nullable=True))
        batch.create_unique_constraint(
            "uq_context_revisions_task_id_reason_run_id_lease_id",
            ["task_id", "reason_run_id", "lease_id"],
        )
        batch.create_index(
            "ix_context_revisions_task_reason_run",
            ["task_id", "reason_run_id"],
            unique=False,
        )

    with op.batch_alter_table("goal_evaluations") as batch:
        batch.add_column(sa.Column("reason_run_id", sa.String(), nullable=True))

    with op.batch_alter_table("audit_events") as batch:
        batch.add_column(sa.Column("reason_run_id", sa.String(), nullable=True))

    with op.batch_alter_table("runtime_settings") as batch:
        batch.add_column(
            sa.Column("applied_version", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("applied_at", sa.String(), nullable=True))

    _migrate_runtime_json(op.get_bind())


def downgrade() -> None:
    connection = op.get_bind()
    _materialize_legacy_reason_intents(connection)
    _downgrade_runtime_json(connection)

    with op.batch_alter_table("runtime_settings") as batch:
        batch.drop_column("applied_at")
        batch.drop_column("applied_version")

    with op.batch_alter_table("audit_events") as batch:
        batch.drop_column("reason_run_id")

    with op.batch_alter_table("goal_evaluations") as batch:
        batch.drop_column("reason_run_id")

    with op.batch_alter_table("context_revisions") as batch:
        batch.drop_index("ix_context_revisions_task_reason_run")
        batch.drop_constraint(
            "uq_context_revisions_task_id_reason_run_id_lease_id",
            type_="unique",
        )
        batch.drop_column("reason_run_id")

    with op.batch_alter_table("reason_workspaces") as batch:
        batch.drop_constraint(
            "uq_reason_workspaces_task_id_reason_run_id_lease_id",
            type_="unique",
        )
        batch.drop_constraint(
            "uq_reason_workspaces_task_id_intent_id_lease_id",
            type_="unique",
        )
        batch.drop_constraint("ck_reason_workspaces_owner", type_="check")
        batch.drop_constraint(
            "fk_reason_workspaces_task_id_reason_run_id_reason_runs",
            type_="foreignkey",
        )
        batch.drop_column("reason_run_id")
        batch.alter_column("intent_id", existing_type=sa.String(), nullable=False)
        batch.create_unique_constraint(
            "uq_reason_workspaces_task_id_intent_id_lease_id",
            ["task_id", "intent_id", "lease_id"],
        )

    op.drop_index("ix_reason_run_sources_task_fact", table_name="reason_run_sources")
    op.drop_table("reason_run_sources")
    op.drop_index("uq_reason_runs_one_open_per_task", table_name="reason_runs")
    op.drop_index("ix_reason_runs_retry", table_name="reason_runs")
    op.drop_index("ix_reason_runs_task_status", table_name="reason_runs")
    op.drop_table("reason_runs")


def _materialize_legacy_reason_intents(connection) -> None:
    """Preserve ReasonRun audit data when rolling back to the Intent-owned schema."""

    runs = connection.execute(
        sa.text(
            """
            SELECT *
            FROM reason_runs
            ORDER BY task_id, created_at, id
            """
        )
    ).mappings().all()
    next_values: dict[str, int] = {}

    for run in runs:
        task_id = run["task_id"]
        if task_id not in next_values:
            counter = connection.execute(
                sa.text(
                    """
                    SELECT next_value
                    FROM id_counters
                    WHERE scope = :task_id AND kind = 'intent'
                    """
                ),
                {"task_id": task_id},
            ).scalar_one_or_none()
            existing = connection.execute(
                sa.text("SELECT id FROM intents WHERE task_id = :task_id"),
                {"task_id": task_id},
            ).scalars()
            numeric_ids = [
                int(intent_id[1:])
                for intent_id in existing
                if intent_id.startswith("i") and intent_id[1:].isdigit()
            ]
            next_values[task_id] = max(counter or 1, max(numeric_ids, default=0) + 1)

        sequence = next_values[task_id]
        while connection.execute(
            sa.text(
                """
                SELECT 1
                FROM intents
                WHERE task_id = :task_id AND id = :intent_id
                """
            ),
            {"task_id": task_id, "intent_id": f"i{sequence:03d}"},
        ).scalar_one_or_none():
            sequence += 1
        intent_id = f"i{sequence:03d}"
        next_values[task_id] = sequence + 1

        achieved = bool(
            connection.execute(
                sa.text(
                    """
                    SELECT 1
                    FROM goal_evaluations
                    WHERE task_id = :task_id
                      AND reason_run_id = :reason_run_id
                      AND achieved = 1
                    LIMIT 1
                    """
                ),
                {"task_id": task_id, "reason_run_id": run["id"]},
            ).scalar_one_or_none()
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO intents (
                    id, task_id, todo_id, kind, description, status, creator,
                    worker, worker_pool_json, targets_json,
                    execute_timeout_seconds, conclude_timeout_seconds,
                    lease_id, claimed_at, last_heartbeat_at, retry_count,
                    infrastructure_retry_count, next_retry_at,
                    last_failure_kind, failure_class, activity_kind,
                    activity_summary, activity_tool, activity_started_at,
                    activity_updated_at, to_fact_id, created_at, concluded_at,
                    archived
                ) VALUES (
                    :id, :task_id, NULL, 'reason', :description, :status,
                    'migration.reason_run_downgrade', :worker,
                    :worker_pool_json, '[]', :execute_timeout_seconds,
                    :conclude_timeout_seconds, :lease_id, :claimed_at,
                    :last_heartbeat_at, :retry_count,
                    :infrastructure_retry_count, :next_retry_at,
                    :last_failure_kind, :failure_class, :activity_kind,
                    :activity_summary, :activity_tool, :activity_started_at,
                    :activity_updated_at, :to_fact_id, :created_at,
                    :concluded_at, 0
                )
                """
            ),
            {
                "id": intent_id,
                "task_id": task_id,
                "description": (
                    f"ReasonRun {run['id']} 回滚兼容记录：{run['trigger']}"
                ),
                "status": run["status"],
                "worker": run["worker"],
                "worker_pool_json": run["worker_pool_json"],
                "execute_timeout_seconds": run["execute_timeout_seconds"],
                "conclude_timeout_seconds": run["conclude_timeout_seconds"],
                "lease_id": run["lease_id"],
                "claimed_at": run["claimed_at"],
                "last_heartbeat_at": run["last_heartbeat_at"],
                "retry_count": run["retry_count"],
                "infrastructure_retry_count": run["infrastructure_retry_count"],
                "next_retry_at": run["next_retry_at"],
                "last_failure_kind": run["last_failure_kind"],
                "failure_class": run["failure_class"],
                "activity_kind": run["activity_kind"],
                "activity_summary": run["activity_summary"],
                "activity_tool": run["activity_tool"],
                "activity_started_at": run["activity_started_at"],
                "activity_updated_at": run["activity_updated_at"],
                "to_fact_id": "goal" if achieved else None,
                "created_at": run["created_at"],
                "concluded_at": run["concluded_at"],
            },
        )
        connection.execute(
            sa.text(
                """
                INSERT OR IGNORE INTO intent_sources (task_id, intent_id, fact_id)
                SELECT task_id, :intent_id, fact_id
                FROM reason_run_sources
                WHERE task_id = :task_id AND reason_run_id = :reason_run_id
                """
            ),
            {
                "task_id": task_id,
                "reason_run_id": run["id"],
                "intent_id": intent_id,
            },
        )
        owner = {
            "task_id": task_id,
            "reason_run_id": run["id"],
            "intent_id": intent_id,
        }
        for table in (
            "reason_workspaces",
            "context_revisions",
            "goal_evaluations",
            "audit_events",
        ):
            connection.execute(
                sa.text(
                    f"""
                    UPDATE {table}
                    SET intent_id = :intent_id, reason_run_id = NULL
                    WHERE task_id = :task_id AND reason_run_id = :reason_run_id
                    """
                ),
                owner,
            )

    for task_id, next_value in next_values.items():
        connection.execute(
            sa.text(
                """
                INSERT INTO id_counters (scope, kind, next_value)
                VALUES (:task_id, 'intent', :next_value)
                ON CONFLICT(scope, kind)
                DO UPDATE SET next_value = excluded.next_value
                """
            ),
            {"task_id": task_id, "next_value": next_value},
        )


def _downgrade_runtime_json(connection) -> None:
    new_only = {
        "global_max_active_runs",
        "global_max_active_tasks",
        "per_task_max_active_runs",
        "per_task_explore_concurrency",
        "reason_conclude_seconds",
        "reason_min_interval_seconds",
        "reason_max_new_intents_per_run",
        "explore_execute_min_seconds",
        "explore_execute_max_seconds",
        "explore_conclude_min_seconds",
        "explore_conclude_max_seconds",
        "guard_whitelist_tools",
        "guard_whitelist_bash_verbs",
        "guard_hard_block_rules",
        "guard_system_prompt",
    }
    rows = connection.execute(
        sa.text("SELECT id, config_json FROM runtime_settings")
    ).mappings()
    for row in rows:
        try:
            payload = json.loads(row["config_json"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        payload["max_parallel"] = payload.get(
            "global_max_active_runs",
            payload.get("max_parallel", 8),
        )
        for key in new_only:
            payload.pop(key, None)
        connection.execute(
            sa.text("UPDATE runtime_settings SET config_json = :config WHERE id = :id"),
            {
                "id": row["id"],
                "config": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            },
        )


def _migrate_runtime_json(connection) -> None:
    defaults = {
        "runner_interval_seconds": 3.0,
        "global_max_active_runs": 10,
        "global_max_active_tasks": 3,
        "per_task_max_active_runs": 4,
        "per_task_explore_concurrency": 4,
        "heartbeat_interval_seconds": 10,
        "lease_timeout_seconds": 60,
        "bootstrap_execute_seconds": 300,
        "bootstrap_conclude_seconds": 120,
        "reason_execute_seconds": 300,
        "reason_conclude_seconds": 120,
        "reason_batch_size": 2,
        "reason_low_watermark": 1,
        "reason_max_wait_seconds": 180,
        "reason_min_interval_seconds": 60,
        "reason_target_open_intents": 4,
        "reason_max_new_intents_per_run": 4,
        "explore_execute_min_seconds": 60,
        "explore_execute_max_seconds": 3600,
        "explore_conclude_min_seconds": 30,
        "explore_conclude_max_seconds": 600,
        "guard_whitelist_tools": ["read", "ls", "grep", "find"],
        "guard_whitelist_bash_verbs": [
            "pwd", "whoami", "id", "uname", "ls", "cat", "head", "tail",
            "grep", "find", "ifconfig", "ip", "env", "printenv",
        ],
        "guard_hard_block_rules": [
            {
                "reason": "修改或重置用户密码",
                "pattern": r"\b(passwd|chpasswd|reset[_-]?password)\b",
                "enabled": True,
            },
            {
                "reason": "删除或清空关键数据",
                "pattern": r"\b(drop\s+table|truncate\s+table|redis-cli\s+flush(all|db))\b|rm\s+-rf\s+(/|/etc|/var|/usr|/opt|/home|\*)",
                "enabled": True,
            },
            {
                "reason": "停止或重启业务服务",
                "pattern": r"\b(systemctl\s+(stop|restart)|shutdown|reboot|service\s+\S+\s+(stop|restart))\b",
                "enabled": True,
            },
            {
                "reason": "篡改账户或权限体系",
                "pattern": r"\b(useradd|userdel|usermod|groupadd|groupdel|grant\s+all|revoke\s+all)\b",
                "enabled": True,
            },
            {
                "reason": "高并发或 DoS 操作",
                "pattern": r"\b(slowloris|hping3|ab\s+-n\s+\d{5,}|wrk\s+.*-c\s+\d{3,})\b",
                "enabled": True,
            },
        ],
        "guard_system_prompt": "你是授权渗透测试的安全守门员。默认放行正常的漏洞探测与最小化验证；只有操作会直接破坏业务可用性、不可逆修改真实数据、触达真实用户权益、造成高并发拒绝服务或明显越过授权边界时才阻断。只回复 ALLOW 或 BLOCK:<不超过30字的原因>。",
    }
    obsolete = {
        "max_parallel",
        "max_workers",
        "max_running_tasks",
        "max_task_workers",
        "bootstrap_limit",
        "reason_limit",
        "explore_limit",
        "bootstrap_worker_count",
        "reason_worker_count",
        "explore_worker_count",
        "plan_worker_count",
        "explore_execute_seconds",
        "explore_conclude_seconds",
    }
    rows = connection.execute(
        sa.text("SELECT id, config_json FROM runtime_settings")
    ).mappings()
    for row in rows:
        try:
            payload = json.loads(row["config_json"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        if "global_max_active_runs" not in payload:
            payload["global_max_active_runs"] = payload.get("max_parallel", 10)
        if payload.get("guard_mode") == "observe":
            payload["guard_mode"] = "audit"
        elif payload.get("guard_mode") == "guarded":
            payload["guard_mode"] = "enforce"
        for key, value in defaults.items():
            payload.setdefault(key, value)
        for key in obsolete:
            payload.pop(key, None)
        connection.execute(
            sa.text("UPDATE runtime_settings SET config_json = :config WHERE id = :id"),
            {
                "id": row["id"],
                "config": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            },
        )
