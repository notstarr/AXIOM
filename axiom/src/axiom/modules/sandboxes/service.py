from __future__ import annotations

import json
import os
import shutil
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
                    root_path=str(root if model.backend == "local" else "/workspace/axiom"),
                    backend=model.backend,
                )
                self.repository.touch(conn, task_id=task_id, now=now)
                return model, handle
            if row is not None and row["status"] in {"creating", "terminating"}:
                raise ConflictError(f"Task Sandbox is currently {row['status']}: {task_id}")
            sandbox_pk = allocate_id(conn, scope=task_id, kind="sandbox", prefix="sb")
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
                "resource_profile_json": json.dumps(self.resource_profile, ensure_ascii=False),
                "network_policy_json": json.dumps(self.network_policy, ensure_ascii=False),
                "active_worker_count": 0,
                "last_used_at": now,
                "terminated_at": None,
                "last_error": "",
            }
            if row is None:
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
            return TaskSandbox(
                id=sandbox_pk,
                task_id=task_id,
                sandbox_id=handle.sandbox_id,
                backend=handle.backend,
                status="ready",
                template_version=self.template_version,
                resource_profile=self.resource_profile,
                network_policy=self.network_policy,
                created_at=now,
                last_used_at=now,
            ), handle

    def run_directory(self, *, task_id: str, owner_id: str, lease_id: str) -> Path:
        _, handle = self.ensure(task_id)
        backend = self._backend_for_handle(handle)
        path = backend.run_directory(handle, owner_id=owner_id, lease_id=lease_id)
        # Local paths are created by LocalExecutionBackend.  A remote path is
        # intentionally not mkdir'ed on the control plane.
        return path

    def cleanup_run_directory(self, *, task_id: str, owner_id: str, lease_id: str) -> None:
        """Remove only a successfully committed run's private directory."""
        sandbox = self.get(task_id)
        if sandbox is None or sandbox.status != "ready":
            return
        if sandbox.backend != "local":
            # Remote backends own their filesystem.  The remote cleanup hook
            # is intentionally optional; never delete a control-plane path
            # that merely resembles the remote POSIX location.
            return
        handle = SandboxHandle(
            sandbox_id=sandbox.sandbox_id,
            root_path=str(self.store.resolve(f"sandboxes/{task_id}")),
            backend=sandbox.backend,
        )
        for value in (owner_id, lease_id):
            if not value or "/" in value or "\\" in value or value in {".", ".."}:
                raise ConflictError("Invalid sandbox cleanup owner")
        path = Path(handle.root_path) / "workspace" / "axiom" / "runs" / owner_id / lease_id
        root = Path(handle.root_path).resolve()
        resolved = path.resolve()
        if root not in resolved.parents or resolved == root:
            raise ConflictError("Invalid sandbox cleanup path")
        shutil.rmtree(resolved, ignore_errors=True)

    def destroy(self, task_id: str) -> None:
        with transaction(self.engine) as conn:
            row = self.repository.get_for_task(conn, task_id, for_update=True)
            if row is None:
                return
            model = TaskSandbox.model_validate(self.repository.to_model(row))
            handle = SandboxHandle(
                sandbox_id=model.sandbox_id,
                root_path=str(self.store.resolve(f"sandboxes/{task_id}")),
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
                root_path=str(self.store.resolve(f"sandboxes/{task_id}")),
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
