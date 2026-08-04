from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from axiom.shared.errors import ConflictError, NotFoundError


_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass(slots=True)
class PreparedFile:
    relative_name: str
    storage_key: str
    temporary_path: Path
    final_path: Path
    content_hash: str
    size: int


@dataclass(slots=True)
class PreparedFiles:
    staging_dir: Path
    files: list[PreparedFile]
    cleanup_paths: list[Path] = field(default_factory=list)
    _moved: list[Path] = field(default_factory=list)

    def commit(self) -> None:
        for item in self.files:
            item.final_path.parent.mkdir(parents=True, exist_ok=True)
            if item.final_path.exists():
                existing_hash = _sha256(item.final_path)
                if existing_hash != item.content_hash:
                    raise ConflictError(f"Artifact path already exists: {item.storage_key}")
                item.temporary_path.unlink(missing_ok=True)
                continue
            os.replace(item.temporary_path, item.final_path)
            self._moved.append(item.final_path)

    def rollback(self) -> None:
        for path in reversed(self._moved):
            path.unlink(missing_ok=True)
        shutil.rmtree(self.staging_dir, ignore_errors=True)

    def finish(self) -> None:
        for path in self.cleanup_paths:
            path.unlink(missing_ok=True)
        shutil.rmtree(self.staging_dir, ignore_errors=True)


class FileStore:
    """Safe, relocatable storage rooted outside the source tree."""

    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / ".staging").mkdir(exist_ok=True)

    def prepare_text_files(
        self,
        *,
        task_id: str,
        intent_id: str,
        files: dict[str, str],
    ) -> PreparedFiles:
        _validate_segment(task_id, "task_id")
        _validate_segment(intent_id, "intent_id")
        return self.prepare_named_text_files(
            base_key=f"tasks/{task_id}/artifacts/{intent_id}",
            files=files,
        )

    def prepare_named_text_files(
        self,
        *,
        base_key: str,
        files: dict[str, str],
    ) -> PreparedFiles:
        return self.prepare_named_files(
            base_key=base_key,
            files={name: content.encode("utf-8") for name, content in files.items()},
        )

    def prepare_named_files(
        self,
        *,
        base_key: str,
        files: dict[str, bytes],
    ) -> PreparedFiles:
        base = PurePosixPath(base_key)
        if base.is_absolute() or not base.parts or ".." in base.parts:
            raise ConflictError("Invalid storage base path")
        staging_dir = Path(tempfile.mkdtemp(prefix="artifact-", dir=self.root / ".staging"))
        prepared: list[PreparedFile] = []
        try:
            for filename, payload in files.items():
                relative_file = _validate_relative_file(filename)
                temporary_path = staging_dir / Path(*relative_file.parts)
                temporary_path.parent.mkdir(parents=True, exist_ok=True)
                with temporary_path.open("wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                storage_key = str(base / relative_file)
                prepared.append(
                    PreparedFile(
                        relative_name=str(relative_file),
                        storage_key=storage_key,
                        temporary_path=temporary_path,
                        final_path=self.resolve(storage_key),
                        content_hash=hashlib.sha256(payload).hexdigest(),
                        size=len(payload),
                    )
                )
        except BaseException:
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise
        return PreparedFiles(staging_dir=staging_dir, files=prepared)

    def prepare_existing_files(
        self,
        *,
        base_key: str,
        files: dict[str, Path],
    ) -> PreparedFiles:
        """Stage validated worker outputs for durable Artifact storage.

        The worker-owned source files remain in place until the database
        transaction succeeds.  ``finish`` removes them only after the staged
        copies have been committed, so a failed closeout never destroys the
        evidence needed by the bounded conclude pass or a later retry.
        """

        base = PurePosixPath(base_key)
        if base.is_absolute() or not base.parts or ".." in base.parts:
            raise ConflictError("Invalid storage base path")
        staging_dir = Path(tempfile.mkdtemp(prefix="artifact-", dir=self.root / ".staging"))
        prepared: list[PreparedFile] = []
        cleanup_paths: list[Path] = []
        try:
            for filename, source_path in files.items():
                relative_file = _validate_relative_file(filename)
                source_path = source_path.resolve(strict=True)
                if not source_path.is_file():
                    raise ConflictError(f"Artifact source is not a file: {filename}")
                temporary_path = staging_dir / Path(*relative_file.parts)
                temporary_path.parent.mkdir(parents=True, exist_ok=True)
                with source_path.open("rb") as source, temporary_path.open("wb") as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
                    target.flush()
                    os.fsync(target.fileno())
                storage_key = str(base / relative_file)
                prepared.append(
                    PreparedFile(
                        relative_name=str(relative_file),
                        storage_key=storage_key,
                        temporary_path=temporary_path,
                        final_path=self.resolve(storage_key),
                        content_hash=_sha256(temporary_path),
                        size=temporary_path.stat().st_size,
                    )
                )
                cleanup_paths.append(source_path)
        except BaseException:
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise
        return PreparedFiles(
            staging_dir=staging_dir,
            files=prepared,
            cleanup_paths=cleanup_paths,
        )

    def read_text(self, storage_key: str) -> str:
        path = self.resolve(storage_key)
        if not path.is_file():
            raise NotFoundError(f"Artifact file not found: {storage_key}")
        return path.read_text(encoding="utf-8")

    def read_bytes(self, storage_key: str) -> bytes:
        path = self.resolve(storage_key)
        if not path.is_file():
            raise NotFoundError(f"Artifact file not found: {storage_key}")
        return path.read_bytes()

    def run_directory(
        self,
        *,
        task_id: str,
        lease_id: str,
        intent_id: str | None = None,
        reason_run_id: str | None = None,
        review_run_id: str | None = None,
    ) -> Path:
        _validate_segment(task_id, "task_id")
        _validate_segment(lease_id, "lease_id")
        owners = [item for item in (intent_id, reason_run_id, review_run_id) if item]
        if len(owners) != 1:
            raise ConflictError(
                "Run directory requires exactly one Intent, ReasonRun or ReviewRun owner"
            )
        owner_id = owners[0]
        assert owner_id is not None
        _validate_segment(owner_id, "execution owner")
        path = self.resolve(f"runs/{task_id}/{owner_id}/{lease_id}")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def delete(self, storage_key: str) -> None:
        self.resolve(storage_key).unlink(missing_ok=True)

    def resolve(self, storage_key: str) -> Path:
        relative = PurePosixPath(storage_key)
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise ConflictError("Invalid storage path")
        path = (self.root / Path(*relative.parts)).resolve()
        if path != self.root and self.root not in path.parents:
            raise ConflictError("Storage path escapes the data directory")
        return path


def _validate_segment(value: str, label: str) -> None:
    if not _SAFE_SEGMENT.fullmatch(value):
        raise ConflictError(f"Invalid {label}")


def _validate_relative_file(value: str) -> PurePosixPath:
    relative = PurePosixPath(value)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ConflictError("Invalid filename")
    for segment in relative.parts:
        _validate_segment(segment, "filename")
    return relative


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
