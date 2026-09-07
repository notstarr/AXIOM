from __future__ import annotations

from pathlib import Path

from axiom.modules.sandboxes.backend import LocalExecutionBackend


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
