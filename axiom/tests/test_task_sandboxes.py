from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from axiom.modules.sandboxes.backend import LocalExecutionBackend
from axiom.modules.sandboxes.service import TaskSandboxManager
from axiom.modules.sandboxes.tables import task_sandboxes
from axiom.shared.storage import FileStore


def test_local_backend_uses_task_scoped_parallel_run_directories(tmp_path: Path) -> None:
    backend = LocalExecutionBackend()
    handle = backend.create(
        task_id="t001",
        root=tmp_path / "t001",
        template_version="test",
        resource_profile={},
        network_policy={},
    )
    first = backend.run_directory(handle, owner_id="i001", lease_id="l001")
    second = backend.run_directory(handle, owner_id="i002", lease_id="l002")

    assert first != second
    assert first.is_dir() and second.is_dir()
    assert first.parent.name == "i001"
    assert second.parent.name == "i002"
    assert handle.sandbox_id == "local:t001"


def test_second_task_sandbox_survives_legacy_id_collision(client, engine, data_dir) -> None:
    """旧版按 task 计数产生 sb001，与全局 PK 冲突导致第二个任务 500。"""
    manager = TaskSandboxManager(engine, FileStore(data_dir))
    ids: list[str] = []
    for number in (1, 2):
        project = client.post("/api/v1/projects", json={"title": f"沙箱冲突{number}"}).json()
        task = client.post(
            "/api/v1/tasks",
            json={
                "project_id": project["id"],
                "title": "沙箱冲突",
                "origin": "a.example.com",
                "goal": "验证沙箱 id 全局唯一",
            },
        ).json()
        manager.ensure(task["id"])
        with engine.begin() as conn:
            ids.append(
                conn.execute(
                    select(task_sandboxes.c.id).where(task_sandboxes.c.task_id == task["id"])
                ).scalar_one()
            )

    assert len(set(ids)) == 2
    assert all(i.startswith("sb") for i in ids)
