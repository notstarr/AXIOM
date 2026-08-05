from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import Engine

from axiom.modules.artifacts.repository import ArtifactRepository
from axiom.modules.artifacts.schemas import Artifact, ArtifactContent, ArtifactFolder
from axiom.modules.tasks.repository import TaskRepository
from axiom.shared.clock import utcnow
from axiom.shared.errors import ConflictError, NotFoundError
from axiom.shared.storage import FileStore

if TYPE_CHECKING:
    from axiom.modules.archives.service import ArchiveService


class ArtifactService:
    def __init__(
        self,
        engine: Engine,
        store: FileStore,
        *,
        archives: ArchiveService | None = None,
    ):
        self.engine = engine
        self.store = store
        self.repository = ArtifactRepository()
        self.tasks = TaskRepository()
        self.archives = archives

    def list(self, task_id: str) -> list[Artifact]:
        with self.engine.connect() as conn:
            task = self.tasks.get(conn, task_id)
            if task is None:
                raise NotFoundError(f"Task not found: {task_id}")
            if task.archived:
                raise ConflictError("已归档任务的产物不可见，请先恢复任务")
            return self.repository.list(conn, task_id)

    def list_page(
        self,
        *,
        task_id: str | None = None,
        project_id: str | None = None,
        search: str = "",
        artifact_type: str | None = None,
        phase: str | None = None,
        source: str | None = None,
        intent_id: str | None = None,
        unscoped_only: bool = False,
        limit: int = 60,
        offset: int = 0,
    ) -> tuple[list[Artifact], int]:
        if intent_id is not None and unscoped_only:
            raise ConflictError("intent_id and unscoped_only are mutually exclusive")
        with self.engine.connect() as conn:
            if task_id is not None:
                task = self.tasks.get(conn, task_id)
                if task is None:
                    raise NotFoundError(f"Task not found: {task_id}")
                if task.archived:
                    raise ConflictError("已归档任务的产物不可见，请先恢复任务")
            return self.repository.list_page(
                conn,
                task_id=task_id,
                project_id=project_id,
                search=search,
                artifact_type=artifact_type,
                phase=phase,
                source=source,
                intent_id=intent_id,
                unscoped_only=unscoped_only,
                limit=limit,
                offset=offset,
            )

    def list_folders(
        self,
        task_id: str,
        *,
        search: str = "",
        limit: int = 60,
        offset: int = 0,
    ) -> tuple[list[ArtifactFolder], int]:
        with self.engine.connect() as conn:
            task = self.tasks.get(conn, task_id)
            if task is None:
                raise NotFoundError(f"Task not found: {task_id}")
            if task.archived:
                raise ConflictError("已归档任务的产物不可见，请先恢复任务")
            return self.repository.list_folders(
                conn,
                task_id=task_id,
                search=search,
                limit=limit,
                offset=offset,
            )

    def get(self, task_id: str, artifact_id: str) -> Artifact:
        with self.engine.connect() as conn:
            task = self.tasks.get(conn, task_id)
            if task is None:
                raise NotFoundError(f"Task not found: {task_id}")
            if task.archived:
                raise ConflictError("已归档任务的产物不可见，请先恢复任务")
            artifact = self.repository.get(conn, task_id, artifact_id)
        if artifact is None:
            raise NotFoundError(f"Artifact not found: {task_id}/{artifact_id}")
        return artifact

    def content(self, task_id: str, artifact_id: str) -> ArtifactContent:
        with self.engine.connect() as conn:
            task = self.tasks.get(conn, task_id)
            if task is None:
                raise NotFoundError(f"Task not found: {task_id}")
            if task.archived:
                raise ConflictError("已归档任务的产物不可见，请先恢复任务")
            artifact = self.repository.get(conn, task_id, artifact_id)
        if artifact is None:
            raise NotFoundError(f"Artifact not found: {task_id}/{artifact_id}")
        return ArtifactContent(
            artifact=artifact,
            content=self.store.read_text(artifact.storage_key),
        )

    def path(self, task_id: str, artifact_id: str) -> tuple[Artifact, Path]:
        with self.engine.connect() as conn:
            task = self.tasks.get(conn, task_id)
            if task is None:
                raise NotFoundError(f"Task not found: {task_id}")
            if task.archived:
                raise ConflictError("已归档任务的产物不可见，请先恢复任务")
            artifact = self.repository.get(conn, task_id, artifact_id)
        if artifact is None:
            raise NotFoundError(f"Artifact not found: {task_id}/{artifact_id}")
        path = self.store.resolve(artifact.storage_key)
        if not path.is_file():
            raise NotFoundError(f"Artifact file not found: {artifact.storage_key}")
        return artifact, path

    def upload(
        self,
        task_id: str,
        *,
        filename: str,
        content: bytes,
        created_by: str,
        summary: str = "",
    ) -> Artifact:
        safe_name = Path(filename).name.strip()
        if not safe_name or safe_name in {".", ".."} or safe_name != filename.strip():
            raise ConflictError("Upload filename must be one safe file name")
        if not content:
            raise ConflictError("Upload file must not be empty")
        actor = created_by.strip() or "human"
        prepared = self.store.prepare_named_files(
            base_key=f"tasks/{task_id}/uploads/{uuid4().hex}",
            files={safe_name: content},
        )
        try:
            with self.engine.begin() as conn:
                task = self.tasks.get(conn, task_id)
                if task is None:
                    raise NotFoundError(f"Task not found: {task_id}")
                if task.legacy_readonly:
                    raise ConflictError("Legacy Task is read-only")
                if task.archived:
                    raise ConflictError("已归档任务不能上传产物，请先恢复任务")
                prepared.commit()
                artifact = self.repository.create_upload(
                    conn,
                    task_id=task_id,
                    prepared=prepared.files[0],
                    summary=summary.strip(),
                    created_by=actor,
                    now=utcnow(),
                )
        except BaseException:
            prepared.rollback()
            raise
        else:
            prepared.finish()
            return artifact

    def archive(self, task_id: str, artifact_id: str) -> Artifact:
        if self.archives is None:
            raise ConflictError("Archive service is unavailable")
        self.archives.create_compat(task_id, root_type="artifact", root_id=artifact_id)
        with self.engine.connect() as conn:
            artifact = self.repository.get_any(conn, task_id, artifact_id)
        assert artifact is not None
        return artifact
