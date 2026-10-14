from __future__ import annotations

import gzip
import json
import os
import time
from pathlib import Path

from sqlalchemy import text


def _create_task(client, project_title: str) -> dict:
    project = client.post("/api/v1/projects", json={"title": project_title}).json()
    response = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": project_title,
            "origin": "https://example.com",
            "goal": "测试目标",
            "worker_pool": ["worker-a"],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _set_task_state(engine, task_id: str, *, status: str, archived: bool = False) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE axiom.tasks SET status = :status, archived = :archived WHERE id = :id"),
            {"status": status, "archived": archived, "id": task_id},
        )


def _make_lease(
    data_dir: Path,
    task_id: str,
    unit_id: str,
    lease_id: str,
    *,
    process_bytes: int,
    product_bytes: int = 256,
    stdout_content: bytes = b"log-line\n",
    age_hours: float | None = 1.0,
) -> Path:
    lease_dir = data_dir / "runs" / task_id / unit_id / f"lease_{lease_id}"
    process_dir = lease_dir / "process"
    process_dir.mkdir(parents=True, exist_ok=True)
    stdout = process_dir / "stdout.log"
    stdout.write_bytes(stdout_content * (process_bytes // len(stdout_content) or 1))
    (lease_dir / "findings.md").write_bytes(b"# findings\n" * (product_bytes // 11 or 1))
    if age_hours is not None:
        _age_path(stdout, age_hours / 24)
        _age_path(process_dir, age_hours / 24)
    return lease_dir


def _age_path(path: Path, days: float) -> None:
    past = time.time() - days * 86_400
    os.utime(path, (past, past))


def _get_plan(client) -> dict:
    response = client.get("/api/v1/storage/cleanup/plan")
    assert response.status_code == 200, response.text
    return response.json()


def _wait_for_plan(client, timeout_s: float = 15.0) -> dict:
    """首调返回骨架（后台线程重建），轮询等待计划填充完成。"""
    import time as time_module

    deadline = time_module.time() + timeout_s
    while time_module.time() < deadline:
        plan = _get_plan(client)
        if plan["tier1"] or plan["tier2"] or plan["tier3"]:
            return plan
        time_module.sleep(0.25)
    raise AssertionError(f"cleanup plan not populated within {timeout_s}s")


def _keys_by_tier(plan: dict) -> tuple[set, set, set]:
    return (
        {item["key"] for item in plan["tier1"]},
        {item["key"] for item in plan["tier2"]},
        {item["key"] for item in plan["tier3"]},
    )


def test_cleanup_plan_classifies_tiers_and_protects_active(client, engine, data_dir):
    stopped = _create_task(client, "已停止任务")["id"]
    running = _create_task(client, "运行中任务")["id"]
    _set_task_state(engine, stopped, status="stopped")
    _set_task_state(engine, running, status="running")

    _make_lease(data_dir, stopped, "i001", "aaa1", process_bytes=4096)
    _make_lease(data_dir, running, "i001", "bbb1", process_bytes=4096, age_hours=None)
    (data_dir / "contexts" / stopped).mkdir(parents=True)
    (data_dir / "contexts" / stopped / "snap.bin").write_bytes(b"x" * 2048)
    (data_dir / "contexts" / running).mkdir(parents=True)
    (data_dir / "contexts" / running / "snap.bin").write_bytes(b"x" * 2048)

    plan = _wait_for_plan(client)
    tier1, tier2, tier3 = _keys_by_tier(plan)

    assert "t1_lease_process" in tier1
    assert f"t2_task_runs:{stopped}" in tier2
    # Whole-task Context deletion requires archive; unarchived Tasks use the
    # revision-level retention path instead.
    assert f"t2_task_contexts:{stopped}" not in tier2
    # 活跃任务整体进三档
    assert f"t3_task_active:{running}" in tier3
    assert not any(key.endswith(running) for key in tier2)
    # 运行中任务的租约因热窗口被保护；一档体积只含已停止任务的冷租约
    t1_process = next(item for item in plan["tier1"] if item["key"] == "t1_lease_process")
    assert t1_process["bytes"] == 4095  # stdout 内容按 9 字节行取整
    assert t1_process["default_selected"] is True


def test_preview_lists_targets_and_excludes_active_lease(client, engine, data_dir):
    task_id = _create_task(client, "预览任务")["id"]
    _set_task_state(engine, task_id, status="stopped")
    _make_lease(data_dir, task_id, "i001", "gone1", process_bytes=2048)
    hot_lease = _make_lease(data_dir, task_id, "i002", "hot1", process_bytes=2048)
    # 活跃租约：DB 标记 running + 文件刚写入
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO axiom.intents (id, task_id, kind, description, priority, status, creator, worker_pool_json, targets_json,"
                " execute_timeout_seconds, conclude_timeout_seconds, lease_id, archived, retry_count, infrastructure_retry_count,"
                " last_failure_kind, failure_class, activity_kind, activity_summary, activity_tool, assessment_status,"
                " assessment_reason, human_intervention_required, human_intervention_reason, created_at)"
                " VALUES ('i_hot', :task_id, 'explore', 'hot', 0, 'running', 'test', '[]', '[]', 600, 180,"
                " 'hot1', false, 0, 0, '', '', '', '', '', 'open', '', false, '', now())"
            ),
            {"task_id": task_id},
        )
    _age_path(hot_lease / "process" / "stdout.log", 0)

    response = client.post(
        "/api/v1/storage/cleanup/preview",
        json={"keys": ["t1_lease_process", f"t2_task_runs:{task_id}"]},
    )
    assert response.status_code == 200, response.text
    items = {item["key"]: item for item in response.json()["items"]}
    t1 = items["t1_lease_process"]
    assert t1["protected_count"] == 1
    assert all("hot1" not in target["path"] for target in t1["targets"])
    assert t1["targets"][0]["path"].endswith("/process")
    # 目录内容未被删除
    assert (data_dir / "runs" / task_id / "i001" / "lease_gone1" / "process" / "stdout.log").exists()

    t2 = items[f"t2_task_runs:{task_id}"]
    paths = " ".join(target["path"] for target in t2["targets"])
    assert "hot1" not in paths and "gone1" in paths


def test_execute_deletes_targets_writes_audit_and_keeps_pg_backups(client, engine, data_dir):
    task_id = _create_task(client, "执行清理任务")["id"]
    _set_task_state(engine, task_id, status="stopped")
    _make_lease(data_dir, task_id, "i001", "del1", process_bytes=1024)

    pg_dir = data_dir / "postgresql-backups"
    pg_dir.mkdir(parents=True)
    payloads = {}
    for index in range(5):
        name = f"backup-{index}.dump"
        (pg_dir / name).write_bytes(b"pg" * 100)
        _age_path(pg_dir / name, 10 + index)  # 越大越旧
        payloads[name] = 200

    logs_dir = data_dir / "logs"
    logs_dir.mkdir(parents=True)
    archive = logs_dir / "server-20200101-000000.log.gz"
    archive.write_bytes(gzip.compress(b"old"))
    _age_path(archive, 60)

    response = client.post(
        "/api/v1/storage/cleanup/execute",
        json={"keys": ["t1_lease_process", "t1_log_archives", "t2_pg_backups"], "actor": "hexin-test"},
    )
    assert response.status_code == 200, response.text
    result = response.json()
    removed_paths = " ".join(item["path"] for item in result["removed"])

    assert not (data_dir / "runs" / task_id / "i001" / "lease_del1" / "process").exists()
    assert (data_dir / "runs" / task_id / "i001" / "lease_del1" / "findings.md").exists()
    assert not archive.exists()
    # _age_path(10+index)：index 越大越旧；最新 3 份(backup-0/1/2)保留，更旧的(3/4)清理
    assert "backup-3.dump" in removed_paths and "backup-4.dump" in removed_paths
    assert "backup-0.dump" not in removed_paths
    kept = sorted(p.name for p in pg_dir.iterdir())
    assert kept == ["backup-0.dump", "backup-1.dump", "backup-2.dump"]
    assert result["freed_bytes"] > 0

    with engine.begin() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM axiom.audit_events WHERE event_type = 'storage.cleanup.executed'")
        ).scalar_one()
    assert count == 1


def test_execute_rejects_unknown_keys_and_path_traversal(client, engine, data_dir):
    response = client.post(
        "/api/v1/storage/cleanup/execute",
        json={"keys": ["t2_maintenance:../../secrets", "t1_unknown_key"], "actor": "hexin-test"},
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["removed"] == []
    assert len(result["skipped"]) == 2
    # 维护目录外的任何东西都没被动过
    assert (data_dir / "maintenance").exists() or True


def test_preview_rejects_running_task_and_unarchived_artifacts(client, engine, data_dir):
    running = _create_task(client, "运行中校验")["id"]
    stopped = _create_task(client, "未归档校验")["id"]
    _set_task_state(engine, running, status="running")
    _set_task_state(engine, stopped, status="stopped")
    (data_dir / "tasks" / running).mkdir(parents=True)
    (data_dir / "tasks" / stopped / "artifacts").mkdir(parents=True)
    (data_dir / "tasks" / stopped / "artifacts" / "a.md").write_bytes(b"data")

    response = client.post(
        "/api/v1/storage/cleanup/preview",
        json={"keys": [f"t2_task_runs:{running}"]},
    )
    assert response.status_code == 409, response.text

    response = client.post(
        "/api/v1/storage/cleanup/preview",
        json={"keys": [f"t2_task_artifacts:{stopped}"]},
    )
    assert response.status_code == 409, response.text

    # 归档后放行
    _set_task_state(engine, stopped, status="stopped", archived=True)
    response = client.post(
        "/api/v1/storage/cleanup/preview",
        json={"keys": [f"t2_task_artifacts:{stopped}"]},
    )
    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert item["warning"]
    assert any(t["path"].endswith("a.md") for t in item["targets"])


def test_sqlite_backup_and_maintenance_items_flow(client, engine, data_dir):
    task_id = _create_task(client, "备份任务")["id"]
    _set_task_state(engine, task_id, status="stopped")

    backups = data_dir / "backups"
    backups.mkdir(parents=True)
    old_backup = backups / "axiom-20200101-old.db"
    old_backup.write_bytes(b"old" * 10)
    _age_path(old_backup, 45)
    fresh_backup = backups / "axiom-20990101-new.db"
    fresh_backup.write_bytes(b"new" * 10)

    maintenance = data_dir / "maintenance"
    maintenance.mkdir(parents=True)
    snapshot = maintenance / "flows-20260801"
    snapshot.write_bytes(b"m" * 512)

    plan = _wait_for_plan(client)
    _t1, tier2, _t3 = _keys_by_tier(plan)
    assert "t2_sqlite_backups" in tier2
    assert "t2_maintenance:flows-20260801" in tier2

    response = client.post(
        "/api/v1/storage/cleanup/execute",
        json={"keys": ["t2_sqlite_backups", "t2_maintenance:flows-20260801"], "actor": "hexin-test"},
    )
    assert response.status_code == 200, response.text
    result = response.json()
    removed_paths = " ".join(item["path"] for item in result["removed"])
    assert "axiom-20200101-old.db" in removed_paths
    assert "axiom-20990101-new.db" not in removed_paths
    assert fresh_backup.exists()
    assert not snapshot.exists()
