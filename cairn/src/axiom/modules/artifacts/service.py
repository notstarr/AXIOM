from __future__ import annotations

import hashlib
import os
from pathlib import Path

from sqlalchemy import Engine, select

from axiom.infrastructure.database.metadata import intents
from axiom.modules.artifacts.repository import ArtifactRepository
from axiom.modules.artifacts.schemas import Artifact, RegisterArtifact
from axiom.modules.common.time import utcnow
from axiom.modules.tasks.repository import TaskRepository


class ArtifactService:
    def __init__(self, engine: Engine, *, data_dir: str | Path | None = None):
        self.engine = engine
        self.data_dir = Path(
            data_dir or os.getenv("AXIOM_DATA_DIR") or "~/.local/share/axiom"
        ).expanduser().resolve()
        self.repository = ArtifactRepository()
        self.tasks = TaskRepository()

    def register(self, task_id: str, body: RegisterArtifact) -> Artifact:
        path = Path(body.path).expanduser().resolve()
        try:
            path.relative_to(self.data_dir)
        except ValueError as exc:
            raise ValueError("Artifact path is outside AXIOM_DATA_DIR") from exc
        if not path.is_file():
            raise ValueError(f"Artifact file not found: {path}")
        content_hash = _sha256(path)
        with self.engine.begin() as conn:
            task = self.tasks.get(conn, task_id)
            if task is None:
                raise ValueError(f"Task not found: {task_id}")
            if task.legacy_readonly:
                raise ValueError("Legacy task is read-only")
            if body.intent_id:
                exists = conn.execute(
                    select(intents.c.id).where(
                        intents.c.task_id == task_id,
                        intents.c.id == body.intent_id,
                    )
                ).scalar_one_or_none()
                if exists is None:
                    raise ValueError(f"Intent not found: {body.intent_id}")
            return self.repository.upsert_path(
                conn,
                task_id=task_id,
                path=str(path),
                now=utcnow(),
                values={
                    "intent_id": body.intent_id,
                    "artifact_type": body.artifact_type,
                    "path": str(path),
                    "summary": body.summary or "",
                    "content_hash": content_hash,
                    "size": path.stat().st_size,
                    "created_by": body.actor,
                    "archived": False,
                },
            )

    def list(self, task_id: str) -> list[Artifact]:
        with self.engine.connect() as conn:
            return self.repository.list(conn, task_id)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
