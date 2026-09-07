from __future__ import annotations

import shutil
from pathlib import Path
from typing import Protocol

from axiom.modules.sandboxes.schemas import SandboxHandle
from axiom.shared.errors import ConflictError


class ExecutionBackend(Protocol):
    name: str

    def create(self, *, task_id: str, root: Path, template_version: str, resource_profile: dict, network_policy: dict) -> SandboxHandle:
        ...

    def destroy(self, handle: SandboxHandle) -> None:
        ...

    def run_directory(self, handle: SandboxHandle, *, owner_id: str, lease_id: str) -> Path:
        ...


class LocalExecutionBackend:
    name = "local"

    def create(self, *, task_id: str, root: Path, template_version: str, resource_profile: dict, network_policy: dict) -> SandboxHandle:
        root.mkdir(parents=True, exist_ok=True)
        (root / "workspace" / "axiom" / "runs").mkdir(parents=True, exist_ok=True)
        (root / "workspace" / "axiom" / "contexts").mkdir(parents=True, exist_ok=True)
        (root / "workspace" / "axiom" / "shared" / "skills").mkdir(parents=True, exist_ok=True)
        (root / "workspace" / "axiom" / "shared" / "tool-cache").mkdir(parents=True, exist_ok=True)
        return SandboxHandle(sandbox_id=f"local:{task_id}", root_path=str(root), backend=self.name)

    def destroy(self, handle: SandboxHandle) -> None:
        root = Path(handle.root_path).resolve()
        if root.exists() and root.name == handle.sandbox_id.split(":", 1)[-1]:
            shutil.rmtree(root)

    def run_directory(self, handle: SandboxHandle, *, owner_id: str, lease_id: str) -> Path:
        for value in (owner_id, lease_id):
            if not value or "/" in value or "\\" in value or value in {".", ".."}:
                raise ConflictError("Invalid sandbox run owner")
        path = Path(handle.root_path) / "workspace" / "axiom" / "runs" / owner_id / lease_id
        path.mkdir(parents=True, exist_ok=True)
        return path


class CubeSandboxExecutionBackend:
    """Optional CubeSandbox adapter.

    CubeSandbox exposes an E2B-compatible Python API.  We intentionally keep
    the import lazy: local development and tests do not need the service, but
    production configured with ``sandbox_required=true`` fails closed when the
    SDK or endpoint is unavailable instead of running on the host.
    """

    name = "cube"

    def __init__(self, *, template: str = "axiom-worker", api_key: str = "", base_url: str = ""):
        self.template = template
        self.api_key = api_key
        self.base_url = base_url

    @staticmethod
    def _sdk_class():
        """Load CubeSandbox's native SDK, with E2B compatibility fallback."""
        try:
            from cubesandbox import Sandbox  # type: ignore

            return Sandbox
        except ImportError:
            try:
                from e2b import Sandbox  # type: ignore

                return Sandbox
            except ImportError as exc:
                raise RuntimeError(
                    "CubeSandbox backend requires the cubesandbox/e2b-compatible SDK"
                ) from exc

    def create(self, *, task_id: str, root: Path, template_version: str, resource_profile: dict, network_policy: dict) -> SandboxHandle:
        Sandbox = self._sdk_class()
        kwargs = {}
        if self.api_key:
            kwargs["api_key"] = self.api_key
        if self.base_url:
            kwargs["base_url"] = self.base_url
        sandbox = Sandbox.create(template=self.template, **kwargs)
        sandbox_id = str(getattr(sandbox, "sandbox_id", getattr(sandbox, "id", "")))
        if not sandbox_id:
            raise RuntimeError("CubeSandbox returned no sandbox id")
        # Run paths are remote POSIX paths.  The manager keeps the handle
        # durable; file transfer is performed by the backend implementation in
        # deployments that expose the E2B files API.
        return SandboxHandle(
            sandbox_id=sandbox_id,
            root_path="/workspace/axiom",
            backend=self.name,
            metadata={"task_id": task_id, "template_version": template_version},
        )

    def destroy(self, handle: SandboxHandle) -> None:
        # A concrete deployment may use Sandbox.connect(id).kill().  Keep this
        # adapter conservative when the optional SDK is absent.
        try:
            Sandbox = self._sdk_class()
            sandbox = Sandbox.connect(handle.sandbox_id)
            kill = getattr(sandbox, "kill", None) or getattr(sandbox, "close", None)
            if kill is not None:
                kill()
        except (ImportError, OSError, RuntimeError):
            return

    def run_directory(self, handle: SandboxHandle, *, owner_id: str, lease_id: str) -> Path:
        # The path is consumed by the Worker environment inside the sandbox.
        # Local Path construction remains useful for API/tests and diagnostics.
        return Path(handle.root_path) / "runs" / owner_id / lease_id
