from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path


LOG = logging.getLogger(__name__)


class TaskStorageError(ValueError):
    """Raised when task-owned files could not be removed safely."""


@dataclass(slots=True)
class TaskDeletionReport:
    removed: list[str] = field(default_factory=list)
    skipped_external: list[str] = field(default_factory=list)
    manifest_count: int = 0


class TaskStorage:
    """Owns the on-disk lifecycle of a V2 task.

    A task is spread over several roots because contexts, worker runs and
    uploaded/legacy artifacts have different consumers.  Keeping the paths
    here makes deletion and orphan repair use the same rules instead of
    relying on the database cascade alone.
    """

    def __init__(self, data_dir: str | Path | None = None):
        self.data_dir = Path(
            data_dir or os.getenv("AXIOM_DATA_DIR") or "~/.local/share/axiom"
        ).expanduser().resolve()

    def delete_task_data(
        self,
        task_id: str,
        *,
        project_id: str | None = None,
        artifact_paths: list[str] | tuple[str, ...] = (),
    ) -> TaskDeletionReport:
        self._validate_id(task_id, "task_id")
        if project_id:
            self._validate_id(project_id, "project_id")

        report = TaskDeletionReport()
        candidates = [
            self.data_dir / "contexts" / "tasks" / task_id,
            self.data_dir / "contexts" / "attempts" / task_id,
            self.data_dir / "runs" / task_id,
            self.data_dir / "tasks" / task_id,
        ]
        if project_id:
            candidates.append(self.data_dir / "projects" / project_id / task_id)

        # Registered artifacts are authoritative for files created outside a
        # conventional run directory.  Only paths under AXIOM_DATA_DIR are
        # eligible; an old or corrupted DB row must never turn task deletion
        # into arbitrary filesystem deletion.
        for raw_path in artifact_paths:
            path = self._safe_data_path(raw_path)
            if path is None:
                if raw_path:
                    report.skipped_external.append(str(raw_path))
                continue
            candidates.append(path)

        failures: list[str] = []
        seen: set[Path] = set()
        for candidate in candidates:
            path = candidate.resolve(strict=False)
            if path in seen:
                continue
            seen.add(path)
            try:
                if self._remove(path):
                    report.removed.append(str(path))
            except OSError as exc:
                failures.append(f"{path}: {exc}")

        try:
            report.manifest_count = self._delete_manifests_for_task(task_id)
        except OSError as exc:
            failures.append(f"{self.data_dir / 'contexts' / 'manifests'}: {exc}")

        if failures:
            raise TaskStorageError(
                "Unable to delete all task files; task was kept in the database: "
                + "; ".join(failures)
            )
        if report.skipped_external:
            LOG.warning(
                "task=%s has artifact paths outside AXIOM_DATA_DIR; left untouched: %s",
                task_id,
                report.skipped_external,
            )
        return report

    def delete_project_data(
        self,
        project_id: str,
        *,
        artifact_paths_by_task: dict[str, list[str]],
    ) -> TaskDeletionReport:
        """Remove task-owned data and the project-owned directory tree."""

        self._validate_id(project_id, "project_id")
        report = TaskDeletionReport()
        for task_id in sorted(artifact_paths_by_task):
            task_report = self.delete_task_data(
                task_id,
                project_id=project_id,
                artifact_paths=artifact_paths_by_task[task_id],
            )
            report.removed.extend(task_report.removed)
            report.skipped_external.extend(task_report.skipped_external)
            report.manifest_count += task_report.manifest_count

        project_root = self.data_dir / "projects" / project_id
        try:
            if self._remove(project_root):
                report.removed.append(str(project_root.resolve(strict=False)))
        except OSError as exc:
            raise TaskStorageError(
                f"Unable to delete project files; project was kept in the database: {project_root}: {exc}"
            ) from exc
        return report

    def delete_orphaned_task_data(self, live_task_ids: set[str]) -> TaskDeletionReport:
        """Repair files left by tasks that no longer exist in the V2 DB."""

        live = set(live_task_ids)
        report = TaskDeletionReport()
        failures: list[str] = []
        roots = (
            self.data_dir / "contexts" / "tasks",
            self.data_dir / "contexts" / "attempts",
            self.data_dir / "runs",
            self.data_dir / "tasks",
        )
        for root in roots:
            if not root.is_dir():
                continue
            for child in root.iterdir():
                if not child.name.startswith("t_") or child.name in live:
                    continue
                try:
                    if self._remove(child):
                        report.removed.append(str(child.resolve(strict=False)))
                except OSError as exc:
                    failures.append(f"{child}: {exc}")

        projects_root = self.data_dir / "projects"
        if projects_root.is_dir():
            for project_root in projects_root.iterdir():
                if not project_root.is_dir() or project_root.is_symlink():
                    continue
                for task_root in project_root.iterdir():
                    if not task_root.name.startswith("t_") or task_root.name in live:
                        continue
                    try:
                        if self._remove(task_root):
                            report.removed.append(str(task_root.resolve(strict=False)))
                    except OSError as exc:
                        failures.append(f"{task_root}: {exc}")

        try:
            report.manifest_count = self._delete_orphaned_manifests(live)
        except OSError as exc:
            failures.append(f"{self.data_dir / 'contexts' / 'manifests'}: {exc}")
        if failures:
            raise TaskStorageError("Unable to repair all orphaned task files: " + "; ".join(failures))
        return report

    def _delete_manifests_for_task(self, task_id: str) -> int:
        manifest_dir = self.data_dir / "contexts" / "manifests"
        if not manifest_dir.is_dir():
            return 0
        count = 0
        for path in manifest_dir.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if payload.get("task_id") != task_id:
                continue
            if self._remove(path):
                count += 1
        return count

    def _delete_orphaned_manifests(self, live_task_ids: set[str]) -> int:
        manifest_dir = self.data_dir / "contexts" / "manifests"
        if not manifest_dir.is_dir():
            return 0
        count = 0
        for path in manifest_dir.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            task_id = payload.get("task_id")
            if isinstance(task_id, str) and task_id and task_id not in live_task_ids and self._remove(path):
                count += 1
        return count

    def _safe_data_path(self, raw_path: str) -> Path | None:
        if not raw_path:
            return None
        path = Path(raw_path).expanduser().resolve(strict=False)
        try:
            path.relative_to(self.data_dir)
        except ValueError:
            return None
        return path

    @staticmethod
    def _remove(path: Path) -> bool:
        if path.is_symlink() or path.is_file():
            path.unlink()
            return True
        if path.is_dir():
            shutil.rmtree(path)
            return True
        return False

    @staticmethod
    def _validate_id(value: str, name: str) -> None:
        if not value or value in {".", ".."} or "/" in value or "\\" in value:
            raise ValueError(f"Invalid {name}")
