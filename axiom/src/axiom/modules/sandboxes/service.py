from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, select, update

from axiom.modules.sandboxes.backend import CubeSandboxExecutionBackend, ExecutionBackend, LocalExecutionBackend
from axiom.modules.sandboxes.repository import TaskSandboxRepository
from axiom.modules.sandboxes.schemas import SandboxHandle, TaskSandbox
from axiom.modules.settings.service import load_runtime_config
from axiom.modules.graph.tables import intents
from axiom.modules.reason.tables import reason_runs
from axiom.modules.plan.tables import plan_runs
from axiom.modules.vulnerability_reviews.tables import vulnerability_review_runs
from axiom.modules.sandboxes.tables import task_sandboxes
from axiom.modules.tasks.tables import tasks
from axiom.shared.clock import utcnow
from axiom.shared.database import transaction
from axiom.shared.errors import ConflictError
from axiom.shared.ids import allocate_id
from axiom.shared.storage import FileStore

logger = logging.getLogger(__name__)


class TaskSandboxManager:
    """Own exactly one execution sandbox per Task.

    The manager is deliberately independent from Reason/Plan/Explore.  A
    Worker asks for a run directory by task/owner/lease and receives an
    isolated path; all phase-specific policy stays in the scheduler.
    """

    def __init__(
        self,
        engine: Engine,
        store: FileStore,
        *,
        backend: ExecutionBackend | None = None,
        backend_name: str | None = None,
        sandbox_required: bool | None = None,
        local_fallback: bool | None = None,
        template_version: str = "default",
        resource_profile: dict[str, Any] | None = None,
        network_policy: dict[str, Any] | None = None,
    ):
        self.engine = engine
        self.store = store
        self.repository = TaskSandboxRepository()
        self._backend_explicit = backend is not None or backend_name is not None
        selected = backend_name or os.getenv("AXIOM_SANDBOX_BACKEND", "local")
        self.sandbox_required = (
            sandbox_required
            if sandbox_required is not None
            else os.getenv("AXIOM_SANDBOX_REQUIRED", "0") == "1"
        )
        self.local_fallback = (
            local_fallback
            if local_fallback is not None
            else os.getenv("AXIOM_SANDBOX_LOCAL_FALLBACK", "1") == "1"
        )
        if self.sandbox_required and selected != "cube" and backend is None:
            raise ConflictError("sandbox_required=true requires sandbox_backend=cube")
        self.template_version = template_version
        self.runtime_revision = os.getenv(
            "AXIOM_SANDBOX_RUNTIME_REVISION", "legacy-local"
        )
        self.healthcheck_timeout = 10.0
        self.resource_profile = resource_profile or {}
        self.network_policy = network_policy or {}
        if backend is not None:
            self.backend = backend
        elif selected == "cube":
            self.backend = CubeSandboxExecutionBackend(
                template=os.getenv("AXIOM_CUBE_TEMPLATE", "axiom-worker"),
                api_key=os.getenv("CUBESANDBOX_API_KEY", ""),
                base_url=os.getenv("CUBESANDBOX_BASE_URL", ""),
            )
        else:
            self.backend = LocalExecutionBackend()
        if selected not in {"local", "cube"}:
            raise ConflictError(f"Unsupported sandbox backend: {selected}")

    def _refresh_runtime_config(self) -> None:
        """Apply persisted runtime settings before creating a new sandbox.

        A manager is constructed once with the application container, while
        Runtime settings can be changed through the UI.  Existing sandboxes
        are intentionally never switched in place; this only affects the
        next Task sandbox that is created.  Tests and callers that explicitly
        provide a backend retain full control.
        """
        if self._backend_explicit:
            return
        with self.engine.connect() as conn:
            config = load_runtime_config(conn)
        selected = config.sandbox_backend
        self.sandbox_required = config.sandbox_required
        self.local_fallback = config.sandbox_local_fallback
        self.template_version = config.sandbox_template
        self.runtime_revision = config.sandbox_runtime_revision
        self.healthcheck_timeout = float(config.sandbox_healthcheck_timeout)
        if self.sandbox_required and selected != "cube":
            raise ConflictError("sandbox_required=true requires sandbox_backend=cube")
        if getattr(self.backend, "name", None) == selected:
            return
        if selected == "cube":
            self.backend = CubeSandboxExecutionBackend(
                template=config.sandbox_template,
                api_key=os.getenv("CUBESANDBOX_API_KEY", ""),
                base_url=os.getenv("CUBESANDBOX_BASE_URL", ""),
            )
        elif selected == "local":
            self.backend = LocalExecutionBackend()
        else:
            raise ConflictError(f"Unsupported sandbox backend: {selected}")

    def get(self, task_id: str) -> TaskSandbox | None:
        with self.engine.connect() as conn:
            row = self.repository.get_for_task(conn, task_id)
        return TaskSandbox.model_validate(self.repository.to_model(row)) if row else None

    def ensure(self, task_id: str) -> tuple[TaskSandbox, SandboxHandle]:
        self._refresh_runtime_config()
        now = utcnow()
        root = self.store.resolve(f"sandboxes/{task_id}")
        existing: tuple[TaskSandbox, SandboxHandle] | None = None
        with transaction(self.engine) as conn:
            task_exists = conn.execute(
                select(tasks.c.id).where(tasks.c.id == task_id).with_for_update()
            ).scalar_one_or_none()
            if task_exists is None:
                raise ConflictError(f"Task not found: {task_id}")
            row = self.repository.get_for_task(conn, task_id, for_update=True)
            if row is not None and row["status"] in {"ready", "degraded"}:
                model = TaskSandbox.model_validate(self.repository.to_model(row))
                handle = SandboxHandle(
                    sandbox_id=model.sandbox_id,
                    root_path=str(root if model.backend == "local" else model.remote_root),
                    backend=model.backend,
                )
                # The remote healthcheck happens after this transaction.  If
                # it raises here, SQLAlchemy rolls back the diagnostic update
                # and the broken sandbox incorrectly remains ``ready``.
                existing = (model, handle)
            elif row is not None and row["status"] in {"creating", "terminating"}:
                raise ConflictError(f"Task Sandbox is currently {row['status']}: {task_id}")
            else:
                sandbox_pk = allocate_id(conn, scope="sandbox", kind="sandbox", prefix="sb")
                backend = self.backend
                try:
                    handle = backend.create(
                        task_id=task_id,
                        root=root,
                        template_version=self.template_version,
                        resource_profile=self.resource_profile,
                        network_policy=self.network_policy,
                    )
                except Exception as exc:
                    if backend.name != "cube" or self.sandbox_required or not self.local_fallback:
                        raise
                    # Development fallback is explicit and observable.  It is
                    # disabled by production defaults when CubeSandbox is
                    # required, so a worker can never silently escape isolation.
                    backend = LocalExecutionBackend()
                    handle = backend.create(
                        task_id=task_id,
                        root=root,
                        template_version=self.template_version,
                        resource_profile=self.resource_profile,
                        network_policy=self.network_policy,
                    )
                    self.resource_profile = {
                        **self.resource_profile,
                        "fallback_reason": str(exc),
                    }
                values = {
                    "sandbox_id": handle.sandbox_id,
                    "backend": handle.backend,
                    "status": "ready",
                    "template_version": self.template_version,
                    "runtime_revision": self.runtime_revision,
                    "remote_root": handle.root_path if handle.backend == "cube" else "/workspace/axiom",
                    "resource_profile_json": json.dumps(self.resource_profile, ensure_ascii=False),
                    "network_policy_json": json.dumps(self.network_policy, ensure_ascii=False),
                    "active_worker_count": 0,
                    "last_used_at": now,
                    "terminated_at": None,
                    "last_error": "",
                    "last_healthcheck_at": now,
                    "last_healthcheck_error": "",
                }
                if row is None:
                    # 预检查式分配：INSERT 失败会中止整个 PG 事务，重试必须在
                    # 任何失败语句发生之前完成（历史遗留 sb001 占位问题）。
                    for _attempt in range(8):
                        candidate = allocate_id(conn, scope="sandbox", kind="sandbox", prefix="sb")
                        taken = conn.execute(
                            select(task_sandboxes.c.id).where(task_sandboxes.c.id == candidate)
                        ).scalar_one_or_none()
                        if taken is None:
                            sandbox_pk = candidate
                            break
                    else:
                        raise ConflictError(f"Task Sandbox id allocation conflicted: {task_id}")
                    self.repository.insert(
                        conn,
                        {
                            "id": sandbox_pk,
                            "task_id": task_id,
                            "created_at": now,
                            **values,
                        },
                    )
                else:
                    conn.execute(
                        update(task_sandboxes)
                        .where(task_sandboxes.c.task_id == task_id)
                        .values(**values)
                    )
                    sandbox_pk = row["id"]
                existing = (
                    TaskSandbox(
                        id=sandbox_pk,
                        task_id=task_id,
                        sandbox_id=handle.sandbox_id,
                        backend=handle.backend,
                        status="ready",
                        template_version=self.template_version,
                        runtime_revision=values["runtime_revision"],
                        remote_root=values["remote_root"],
                        resource_profile=self.resource_profile,
                        network_policy=self.network_policy,
                        created_at=now,
                        last_used_at=now,
                        last_healthcheck_at=now,
                    ),
                    handle,
                )

        assert existing is not None
        model, handle = existing
        backend = self._backend_for_handle(handle)
        try:
            try:
                backend.healthcheck(handle, timeout=self.healthcheck_timeout)
            except TypeError:
                # Small fake backends and older adapters may still implement
                # the original one-argument protocol.
                backend.healthcheck(handle)
        except Exception as exc:
            with transaction(self.engine) as conn:
                self.repository.record_healthcheck(
                    conn, task_id=task_id, now=utcnow(), error=str(exc)
                )
            raise ConflictError(f"Task Sandbox healthcheck failed: {exc}") from exc
        checked_at = utcnow()
        with transaction(self.engine) as conn:
            self.repository.record_healthcheck(conn, task_id=task_id, now=checked_at)
        return model.model_copy(
            update={
                "status": "ready",
                "last_healthcheck_at": checked_at,
                "last_healthcheck_error": "",
            }
        ), handle

    def run_directory(self, *, task_id: str, owner_id: str, lease_id: str) -> Path:
        _, handle = self.ensure(task_id)
        backend = self._backend_for_handle(handle)
        path = backend.run_directory(handle, owner_id=owner_id, lease_id=lease_id)
        # Local paths are created by LocalExecutionBackend.  A remote path is
        # intentionally not mkdir'ed on the control plane.
        return path

    def stage_run_directory(self, *, task_id: str, owner_id: str, lease_id: str) -> Path:
        """Materialize a remote Cube run into control-plane staging.

        The returned directory is disposable input to the existing
        FileStore/Artifact transaction.  It is never exposed to the worker.
        """
        sandbox = self.get(task_id)
        if sandbox is None or sandbox.status not in {"ready", "degraded"}:
            raise ConflictError(f"Task Sandbox is unavailable: {task_id}")
        if sandbox.backend == "local":
            return self.run_directory(task_id=task_id, owner_id=owner_id, lease_id=lease_id)
        for value in (owner_id, lease_id):
            if not value or "/" in value or "\\" in value or value in {".", ".."}:
                raise ConflictError("Invalid sandbox staging owner")
        handle = SandboxHandle(
            sandbox_id=sandbox.sandbox_id,
            root_path=sandbox.remote_root,
            backend=sandbox.backend,
        )
        destination = self.store.resolve(
            f"staging/sandbox-runs/{task_id}/{owner_id}/{lease_id}"
        )
        self._backend_for_handle(handle).download_directory(
            handle,
            source=f"runs/{owner_id}/{lease_id}",
            destination=destination,
        )
        return destination

    def upload_run_file(
        self,
        *,
        task_id: str,
        owner_id: str,
        lease_id: str,
        source: Path,
        relative_path: str,
    ) -> None:
        sandbox = self.get(task_id)
        if sandbox is None or sandbox.status not in {"ready", "degraded"}:
            raise ConflictError(f"Task Sandbox is unavailable: {task_id}")
        if sandbox.backend == "local":
            return
        self._validate_run_path(
            f"runs/{owner_id}/{lease_id}/{relative_path}",
            owner_id=owner_id,
            lease_id=lease_id,
        )
        handle = SandboxHandle(
            sandbox_id=sandbox.sandbox_id,
            root_path=sandbox.remote_root,
            backend=sandbox.backend,
        )
        self._backend_for_handle(handle).upload_file(
            handle,
            source=source,
            destination=f"runs/{owner_id}/{lease_id}/{self._relative_worker_path(relative_path)}",
        )

    def cleanup_run_directory(self, *, task_id: str, owner_id: str, lease_id: str) -> None:
        """Remove only a successfully committed run's private directory."""
        sandbox = self.get(task_id)
        if sandbox is None or sandbox.status != "ready":
            return
        handle = SandboxHandle(
            sandbox_id=sandbox.sandbox_id,
                root_path=str(
                    self.store.resolve(f"sandboxes/{task_id}")
                    if sandbox.backend == "local"
                    else sandbox.remote_root
            ),
            backend=sandbox.backend,
        )
        for value in (owner_id, lease_id):
            if not value or "/" in value or "\\" in value or value in {".", ".."}:
                raise ConflictError("Invalid sandbox cleanup owner")
        relative = f"runs/{owner_id}/{lease_id}"
        if sandbox.backend == "local":
            path = Path(handle.root_path) / "workspace" / "axiom" / relative
            root = Path(handle.root_path).resolve()
            resolved = path.resolve()
            if root not in resolved.parents or resolved == root:
                raise ConflictError("Invalid sandbox cleanup path")
            self._preserve_run_outputs(
                run_dir=resolved, task_id=task_id, owner_id=owner_id, lease_id=lease_id
            )
            shutil.rmtree(resolved, ignore_errors=True)
        else:
            logger.warning(
                "cube run dir removed without output preservation: %s/%s", task_id, owner_id
            )
            self._backend_for_handle(handle).remove_path(handle, path=relative)

    _PRESERVED_RUN_SUBDIRS = ("evidence", "artifacts")

    def _preserve_run_outputs(
        self, *, run_dir: Path, task_id: str, owner_id: str, lease_id: str
    ) -> None:
        """Rescue small durable outputs before the run directory is removed.

        Workers write ``evidence/`` (pen-test evidence files) and reviewers write
        ``artifacts/`` (review conclusions + minimal retests) inside the run
        directory. Those must outlive the run: sessions, model snapshots and
        process logs are the bloat cleanup targets, these subdirectories are not.
        """
        for name in self._PRESERVED_RUN_SUBDIRS:
            source = run_dir / name
            if not source.is_dir():
                continue
            destination = self.store.root / "tasks" / task_id / "evidence" / owner_id / lease_id / name
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    shutil.rmtree(destination)
                shutil.move(str(source), str(destination))
            except OSError:
                logger.exception(
                    "failed to preserve run %s for %s/%s; cleanup continues", name, task_id, owner_id
                )

    def file_read(self, *, task_id: str, owner_id: str, lease_id: str, path: str) -> bytes:
        handle, sandbox = self._file_handle(task_id)
        self._validate_run_path(path, owner_id=owner_id, lease_id=lease_id)
        return self._backend_for_handle(handle).read_file(handle, source=path)

    def file_write(
        self,
        *,
        task_id: str,
        owner_id: str,
        lease_id: str,
        path: str,
        content: bytes,
        append: bool = False,
    ) -> None:
        handle, _sandbox = self._file_handle(task_id)
        self._validate_run_path(path, owner_id=owner_id, lease_id=lease_id)
        backend = self._backend_for_handle(handle)
        if append:
            backend.append_file(handle, destination=path, content=content)
        else:
            backend.write_file(handle, destination=path, content=content)

    def file_exists(self, *, task_id: str, owner_id: str, lease_id: str, path: str) -> bool:
        handle, _sandbox = self._file_handle(task_id)
        self._validate_run_path(path, owner_id=owner_id, lease_id=lease_id)
        return self._backend_for_handle(handle).exists(handle, path=path)

    def sync_file(self, *, task_id: str, source: Path, destination: str) -> str:
        """Copy a control-plane file into the Task sandbox and return worker path."""
        if not source.is_file():
            raise ConflictError(f"Sandbox source file is missing: {source}")
        _model, handle = self.ensure(task_id)
        relative = self._relative_worker_path(destination)
        self._backend_for_handle(handle).upload_file(handle, source=source, destination=relative)
        return self._worker_path(handle, relative)

    def sync_directory(self, *, task_id: str, source: Path, destination: str) -> str:
        if not source.is_dir():
            raise ConflictError(f"Sandbox source directory is missing: {source}")
        root = source.resolve()
        for item in root.rglob("*"):
            if item.is_file() and not item.is_symlink():
                relative = Path(destination) / item.relative_to(root)
                self.sync_file(task_id=task_id, source=item, destination=str(relative))
        _model, handle = self.ensure(task_id)
        return self._worker_path(handle, self._relative_worker_path(destination))

    def prepare_worker_assets(
        self,
        *,
        task_id: str,
        context_manifest_path: str | None,
        context_revision: int | None,
        skill_directories: list[str],
        capability_snapshot_path: str | None,
        axiom_tool_snapshot_path: str | None,
        axiom_tool_extensions: list[str],
    ) -> dict[str, Any]:
        """Synchronize the immutable inputs needed by a Cube Worker.

        Local sandboxes intentionally keep the historical host paths.  Cube
        workers receive only paths under ``/workspace/axiom``.
        """
        sandbox = self.get(task_id)
        if sandbox is None or sandbox.backend != "cube":
            return {
                "context_manifest_path": context_manifest_path,
                "skill_directories": skill_directories,
                "capability_snapshot_path": capability_snapshot_path,
                "axiom_tool_snapshot_path": axiom_tool_snapshot_path,
                "axiom_tool_extensions": axiom_tool_extensions,
            }
        context_worker_path = context_manifest_path
        if context_manifest_path and context_revision is not None:
            manifest = Path(context_manifest_path)
            context_worker_path = self.sync_directory(
                task_id=task_id,
                source=manifest.parent,
                destination=f"contexts/{context_revision}",
            ) + "/manifest.json"
        skill_worker_paths: list[str] = []
        path_map: dict[str, str] = {}
        for directory in skill_directories:
            source = Path(directory)
            digest = hashlib.sha256(str(source.resolve()).encode()).hexdigest()[:16]
            worker_path = self.sync_directory(
                    task_id=task_id,
                    source=source,
                    destination=f"shared/skills/{digest}/{source.name}",
                )
            skill_worker_paths.append(worker_path)
            path_map[str(source)] = worker_path
        extension_paths: list[str] = []
        for extension in axiom_tool_extensions:
            source = Path(extension)
            if not source.is_file():
                continue
            package_root = source.parent
            digest = hashlib.sha256(str(package_root.resolve()).encode()).hexdigest()[:16]
            worker_root = self.sync_directory(
                task_id=task_id,
                source=package_root,
                destination=f"shared/axiom-tools/{digest}/{package_root.name}",
            )
            worker_extension = f"{worker_root}/{source.name}"
            extension_paths.append(worker_extension)
            path_map[str(package_root)] = worker_root
            path_map[str(source)] = worker_extension
        cap_worker = None
        if capability_snapshot_path:
            snapshot_source = Path(capability_snapshot_path)
            try:
                payload = json.loads(snapshot_source.read_text(encoding="utf-8"))
                payload = self._rewrite_worker_paths(payload, path_map)
                with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as stream:
                    json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
                    rewritten = Path(stream.name)
                try:
                    cap_worker = self.sync_file(
                        task_id=task_id,
                        source=rewritten,
                        destination=f"shared/capabilities/{snapshot_source.name}",
                    )
                finally:
                    rewritten.unlink(missing_ok=True)
            except (OSError, json.JSONDecodeError):
                cap_worker = self.sync_file(
                    task_id=task_id,
                    source=snapshot_source,
                    destination=f"shared/capabilities/{snapshot_source.name}",
                )
        axiom_worker = self.sync_file(
            task_id=task_id,
            source=Path(axiom_tool_snapshot_path),
            destination=f"shared/capabilities/{Path(axiom_tool_snapshot_path).name}",
        ) if axiom_tool_snapshot_path else None
        return {
            "context_manifest_path": context_worker_path,
            "skill_directories": skill_worker_paths,
            "capability_snapshot_path": cap_worker,
            "axiom_tool_snapshot_path": axiom_worker,
            "axiom_tool_extensions": extension_paths,
        }

    @classmethod
    def _rewrite_worker_paths(cls, value: Any, mapping: dict[str, str]) -> Any:
        if isinstance(value, str):
            for source, target in mapping.items():
                if value == source:
                    return target
                if value.startswith(source.rstrip("/") + "/"):
                    return target.rstrip("/") + value[len(source.rstrip("/")):]
            return value
        if isinstance(value, list):
            return [cls._rewrite_worker_paths(item, mapping) for item in value]
        if isinstance(value, dict):
            return {key: cls._rewrite_worker_paths(item, mapping) for key, item in value.items()}
        return value

    def _file_handle(self, task_id: str) -> tuple[SandboxHandle, TaskSandbox]:
        sandbox = self.get(task_id)
        if sandbox is None or sandbox.status not in {"ready", "degraded"}:
            raise ConflictError(f"Task Sandbox is unavailable: {task_id}")
        handle = SandboxHandle(
            sandbox_id=sandbox.sandbox_id,
            root_path=(
                str(self.store.resolve(f"sandboxes/{task_id}")
                if sandbox.backend == "local"
                else sandbox.remote_root)
            ),
            backend=sandbox.backend,
        )
        return handle, sandbox

    @staticmethod
    def _relative_worker_path(path: str) -> str:
        from posixpath import normpath

        normalized = normpath(str(path).replace("\\", "/").lstrip("/"))
        if normalized in {"", "."} or normalized == ".." or normalized.startswith("../"):
            raise ConflictError("Invalid sandbox worker path")
        return normalized

    @staticmethod
    def _worker_path(handle: SandboxHandle, relative: str) -> str:
        if handle.backend == "cube":
            return f"/workspace/axiom/{relative}"
        return str(Path(handle.root_path) / "workspace" / "axiom" / relative)

    @staticmethod
    def _validate_run_path(path: str, *, owner_id: str, lease_id: str) -> None:
        for value in (owner_id, lease_id):
            if not value or "/" in value or "\\" in value or value in {".", ".."}:
                raise ConflictError("Invalid sandbox run owner")
        relative = TaskSandboxManager._relative_worker_path(path)
        prefix = f"runs/{owner_id}/{lease_id}/"
        if not relative.startswith(prefix):
            raise ConflictError("Sandbox file path is outside the current Lease run directory")

    def destroy(self, task_id: str) -> None:
        with transaction(self.engine) as conn:
            row = self.repository.get_for_task(conn, task_id, for_update=True)
            if row is None:
                return
            model = TaskSandbox.model_validate(self.repository.to_model(row))
            handle = SandboxHandle(
                sandbox_id=model.sandbox_id,
                root_path=str(
                    self.store.resolve(f"sandboxes/{task_id}")
                    if model.backend == "local"
                    else model.remote_root
                ),
                backend=model.backend,
            )
            self._backend_for_handle(handle).destroy(handle)
            self.repository.update_status(conn, task_id=task_id, status="terminated", now=utcnow())

    def destroy_if_idle(self, task_id: str) -> bool:
        """Destroy a Task sandbox only after no run can still reference it."""
        now = utcnow()
        with transaction(self.engine) as conn:
            row = self.repository.get_for_task(conn, task_id, for_update=True)
            if row is None or row["status"] == "terminated":
                return False
            tables = (intents, reason_runs, plan_runs, vulnerability_review_runs)
            busy = 0
            for table in tables:
                busy += int(
                    conn.execute(
                        select(table.c.id)
                        .where(
                            table.c.task_id == task_id,
                            table.c.status.in_(
                                ("pending", "claimed", "running", "concluding", "retry_wait")
                            ),
                        )
                        .limit(1)
                    ).first()
                    is not None
                )
            if busy:
                return False
            model = TaskSandbox.model_validate(self.repository.to_model(row))
            handle = SandboxHandle(
                sandbox_id=model.sandbox_id,
                root_path=str(
                    self.store.resolve(f"sandboxes/{task_id}")
                    if model.backend == "local"
                    else model.remote_root
                ),
                backend=model.backend,
            )
            self.repository.update_status(conn, task_id=task_id, status="terminating", now=now)
            try:
                self._backend_for_handle(handle).destroy(handle)
            except Exception as exc:
                self.repository.update_status(
                    conn, task_id=task_id, status="failed", now=utcnow(), error=str(exc)
                )
                raise
            self.repository.update_status(conn, task_id=task_id, status="terminated", now=utcnow())
            return True

    def _backend_for_handle(self, handle: SandboxHandle) -> ExecutionBackend:
        if handle.backend == getattr(self.backend, "name", None):
            return self.backend
        if handle.backend == "local":
            return LocalExecutionBackend()
        if handle.backend == "cube":
            return CubeSandboxExecutionBackend(
                template=self.template_version,
                api_key=os.getenv("CUBESANDBOX_API_KEY", ""),
                base_url=os.getenv("CUBESANDBOX_BASE_URL", ""),
            )
        raise ConflictError(f"Unsupported sandbox backend: {handle.backend}")
