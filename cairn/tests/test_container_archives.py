from __future__ import annotations

from pathlib import Path

from cairn.dispatcher.config import ContainerConfig
from cairn.dispatcher.runtime.containers import ContainerManager


def test_write_text_file_creates_parent_dirs_and_writes_content(tmp_path: Path) -> None:
    manager = ContainerManager(
        ContainerConfig(image="unused", network_mode="host", completed_action="stop"),
        workspace_root=tmp_path,
    )
    target = tmp_path / "nested" / "graph.yaml"
    manager.write_text_file("local-proj", str(target), "facts:\n- id: f001\n")

    assert target.exists()
    assert target.read_text(encoding="utf-8") == "facts:\n- id: f001\n"


def test_container_name_uses_local_prefix() -> None:
    manager = ContainerManager(
        ContainerConfig(image="unused", network_mode="host", completed_action="stop"),
    )
    assert manager.container_name("proj/001") == "local-proj-001"

