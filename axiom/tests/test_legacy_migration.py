from __future__ import annotations

from axiom.migration.legacy import _next_numeric
from axiom.modules.projects.tables import projects
from axiom.modules.tasks.tables import tasks


def test_counter_rebuild_ignores_noncanonical_legacy_task_ids(engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            projects.insert().values(
                id="prj_001",
                title="迁移",
                description="",
                status="active",
                created_at="2026-07-23T00:00:00Z",
                updated_at="2026-07-23T00:00:00Z",
            )
        )
        values = {
            "project_id": "prj_001",
            "title": "迁移任务",
            "origin_host": "example.com",
            "origin_scheme": "https",
            "origin_port": None,
            "origin_input": "https://example.com",
            "goal": "验证迁移",
            "status": "stopped",
            "phase": "none",
            "scope_json": '{"enabled":true,"targets":["example.com"],"exclude":[]}',
            "worker_pool_json": "[]",
            "guard_enabled": False,
            "external_report_enabled": False,
            "legacy_readonly": True,
            "created_at": "2026-07-23T00:00:00Z",
            "updated_at": "2026-07-23T00:00:00Z",
            "completed_at": None,
        }
        conn.execute(tasks.insert(), [{"id": "t_128", **values}, {"id": "t_legacy_fd8317b7", **values}])

        assert _next_numeric(conn, tasks, "id", "t_", None) == 129
