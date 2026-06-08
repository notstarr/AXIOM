from __future__ import annotations

import logging
from pathlib import Path

from cairn.dispatcher.config import ContainerConfig
from cairn.dispatcher.runtime.process import ManagedProcess

LOG = logging.getLogger(__name__)


class ContainerManager:
    def __init__(self, config: ContainerConfig, *, workspace_root: Path | None = None):
        self._config = config
        self._workspace_root = workspace_root or Path.cwd()

    def close(self) -> None:
        return None

    def container_name(self, project_id: str) -> str:
        # Backward-compatible identifier retained for scheduler/task interfaces.
        sanitized = project_id.replace("/", "-")
        return f"local-{sanitized}"

    def ensure_running(self, project_id: str) -> str:
        return self.container_name(project_id)

    def create_startup_container(self) -> str:
        return "local-startup"

    def inspect_state(self, name: str) -> str | None:
        return "running"

    def cleanup_completed(self, project_id: str) -> bool:
        return True

    def cleanup_stopped(self, project_id: str) -> bool:
        return True

    def cleanup_orphan(self, name: str) -> bool:
        return True

    def managed_container_names(self) -> list[str]:
        return []

    def needs_completed_cleanup(self, project_id: str) -> bool:
        return False

    def needs_orphan_cleanup(self, name: str) -> bool:
        return False

    def needs_stopped_cleanup(self, project_id: str) -> bool:
        return False

    def build_exec_process(
        self,
        container_name: str,
        env: dict[str, str],
        command: list[str],
        timeout_seconds: int | None = None,
        kill_after_seconds: int = 5,
    ) -> ManagedProcess:
        return ManagedProcess(command, env, cwd=str(self._workspace_root))

    def write_text_file(self, container_name: str, path: str, content: str) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def remove_container(self, name: str, *, force: bool = True) -> None:
        LOG.debug("remove_container noop in local mode name=%s force=%s", name, force)
