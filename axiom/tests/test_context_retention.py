from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select, text, update

from axiom.modules.context.retention import ContextRetentionService, RetentionConfig
from axiom.modules.context.tables import context_revisions
from axiom.shared.storage import FileStore


def _task(client, title: str = "Context retention") -> dict:
    project = client.post("/api/v1/projects", json={"title": title}).json()
    response = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": title,
            "origin": "example.com",
            "goal": "保留必要的 Worker 上下文",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _age_tree(path: Path, days: float) -> None:
    timestamp = time.time() - days * 86_400
    for current, directories, files in os.walk(path):
        for name in [*directories, *files]:
            os.utime(Path(current) / name, (timestamp, timestamp))
    os.utime(path, (timestamp, timestamp))


def _retention(engine, data_dir: Path) -> ContextRetentionService:
    return ContextRetentionService(
        engine,
        FileStore(data_dir),
        RetentionConfig(
            enabled=True,
            grace_days=7,
            keep_revisions=3,
            hot_window_minutes=30,
            orphan_grace_hours=24,
            max_revisions=100,
            max_bytes=2 * 1024 * 1024 * 1024,
        ),
    )


def test_terminal_owner_keeps_latest_three_and_reclaims_old_revision(
    client, engine, data_dir
) -> None:
    task = _task(client)
    revisions = []
    for _ in range(4):
        response = client.post(
            f"/api/v1/tasks/{task['id']}/context",
            json={"phase": "bootstrap", "intent_id": "i001"},
        )
        assert response.status_code == 200, response.text
        revisions.append(response.json())
    old = datetime.now(timezone.utc) - timedelta(days=8)
    with engine.begin() as conn:
        conn.execute(
            update(context_revisions)
            .where(context_revisions.c.task_id == task["id"])
            .values(created_at=old)
        )
        conn.execute(
            text("UPDATE axiom.intents SET status = 'completed' WHERE id = 'i001'")
        )
    _age_tree(Path(revisions[0]["manifest_path"]).parent, 8)
    for revision in revisions[1:]:
        _age_tree(Path(revision["manifest_path"]).parent, 8)

    retention = _retention(engine, data_dir)
    plan = retention.plan()
    assert plan.database_records == 4
    assert plan.reclaimable_count == 1
    assert plan.targets[0].revision == revisions[0]["revision"]

    result = retention.run()
    assert [item.revision for item in result.removed] == [revisions[0]["revision"]]
    assert not Path(revisions[0]["manifest_path"]).parent.exists()
    assert Path(revisions[1]["manifest_path"]).parent.exists()
    with engine.connect() as conn:
        row = conn.execute(
            select(context_revisions).where(
                context_revisions.c.task_id == task["id"],
                context_revisions.c.revision == revisions[0]["revision"],
            )
        ).mappings().one()
    assert row["storage_state"] == "reclaimed"
    assert row["deleted_at"] is not None


def test_active_owner_and_recent_revision_are_protected(client, engine, data_dir) -> None:
    task = _task(client, "active Context")
    response = client.post(
        f"/api/v1/tasks/{task['id']}/context",
        json={
            "phase": "bootstrap",
            "intent_id": "i001",
            "lease_id": "lease-active",
        },
    )
    assert response.status_code == 200, response.text
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE axiom.intents SET status = 'running', lease_id = 'lease-active' "
                "WHERE id = 'i001'"
            )
        )
    revision_dir = Path(response.json()["manifest_path"]).parent
    _age_tree(revision_dir, 8)

    retention = _retention(engine, data_dir)
    assert retention.plan().targets == []


def test_orphan_directory_is_reclaimed_and_same_lease_rejects_reclaimed_context(
    client, engine, data_dir
) -> None:
    task = _task(client, "orphan Context")
    orphan = data_dir / "contexts" / task["id"] / "r999999"
    orphan.mkdir(parents=True)
    (orphan / "partial.json").write_text("{}", encoding="utf-8")
    _age_tree(orphan, 2)

    retention = _retention(engine, data_dir)
    plan = retention.plan()
    assert plan.orphan_count == 1
    assert plan.targets[0].kind == "orphan"
    assert retention.run().removed[0].kind == "orphan"
    assert not orphan.exists()

    response = client.post(
        f"/api/v1/tasks/{task['id']}/context",
        json={
            "phase": "bootstrap",
            "intent_id": "i001",
            "lease_id": "lease-reclaim-test",
        },
    )
    assert response.status_code == 200, response.text
    revision = response.json()
    with engine.begin() as conn:
        conn.execute(
            update(context_revisions)
            .where(
                context_revisions.c.task_id == task["id"],
                context_revisions.c.revision == revision["revision"],
            )
            .values(storage_state="reclaimed", deleted_at=datetime.now(timezone.utc))
        )
    response = client.post(
        f"/api/v1/tasks/{task['id']}/context",
        json={
            "phase": "bootstrap",
            "intent_id": "i001",
            "lease_id": "lease-reclaim-test",
        },
    )
    assert response.status_code == 409, response.text
    assert "reclaimed" in response.text


def test_failed_revision_mark_restores_quarantined_directory(client, engine, data_dir, monkeypatch) -> None:
    task = _task(client, "rollback Context")
    response = client.post(
        f"/api/v1/tasks/{task['id']}/context",
        json={"phase": "bootstrap", "intent_id": "i001"},
    )
    assert response.status_code == 200, response.text
    revision = response.json()
    with engine.begin() as conn:
        conn.execute(
            update(context_revisions)
            .where(
                context_revisions.c.task_id == task["id"],
                context_revisions.c.revision == revision["revision"],
            )
            .values(created_at=datetime.now(timezone.utc) - timedelta(days=8))
        )
        conn.execute(text("UPDATE axiom.intents SET status = 'completed' WHERE id = 'i001'"))
    source = Path(revision["manifest_path"]).parent
    _age_tree(source, 8)
    retention = _retention(engine, data_dir)
    monkeypatch.setattr(retention.repository, "mark_reclaimed", lambda *args: False)

    result = retention.run()
    assert result.removed == []
    assert source.exists()
    assert not list((data_dir / ".staging" / "context-gc").glob("*"))
