from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Protocol

from axiom.modules.sandboxes.schemas import SandboxHandle
from axiom.shared.errors import ConflictError


class ExecutionBackend(Protocol):
    name: str

    def create(self, *, task_id: str, root: Path, template_version: str, resource_profile: dict, network_policy: dict) -> SandboxHandle:
        ...

    def destroy(self, handle: SandboxHandle) -> None:
        ...

    def connect(self, handle: SandboxHandle) -> SandboxHandle:
        ...

    def healthcheck(self, handle: SandboxHandle, *, timeout: float = 10.0) -> None:
        ...

    def run_directory(self, handle: SandboxHandle, *, owner_id: str, lease_id: str) -> Path:
        ...

    def upload_file(self, handle: SandboxHandle, *, source: Path, destination: str) -> None:
        ...

    def download_file(self, handle: SandboxHandle, *, source: str, destination: Path) -> None:
        ...

    def remove_path(self, handle: SandboxHandle, *, path: str) -> None:
        ...

    def read_file(self, handle: SandboxHandle, *, source: str) -> bytes:
        ...

    def write_file(self, handle: SandboxHandle, *, destination: str, content: bytes) -> None:
        ...

    def append_file(self, handle: SandboxHandle, *, destination: str, content: bytes) -> None:
        ...

    def exists(self, handle: SandboxHandle, *, path: str) -> bool:
        ...

    def download_directory(self, handle: SandboxHandle, *, source: str, destination: Path) -> None:
        ...

    def open_pty(self, handle: SandboxHandle, *, cwd: str, env: dict[str, str], timeout: float):
        ...

    def pty_send_stdin(self, pty, data: bytes) -> None:
        ...

    def pty_wait(self, pty, on_data):
        ...

    def pty_kill(self, pty) -> None:
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

    def connect(self, handle: SandboxHandle) -> SandboxHandle:
        return handle

    def healthcheck(self, handle: SandboxHandle, *, timeout: float = 10.0) -> None:
        root = Path(handle.root_path).resolve()
        if not root.is_dir():
            raise RuntimeError(f"Local sandbox root is unavailable: {root}")

    def run_directory(self, handle: SandboxHandle, *, owner_id: str, lease_id: str) -> Path:
        for value in (owner_id, lease_id):
            if not value or "/" in value or "\\" in value or value in {".", ".."}:
                raise ConflictError("Invalid sandbox run owner")
        path = Path(handle.root_path) / "workspace" / "axiom" / "runs" / owner_id / lease_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def upload_file(self, handle: SandboxHandle, *, source: Path, destination: str) -> None:
        root = Path(handle.root_path).resolve()
        target = (root / "workspace" / "axiom" / destination.lstrip("/")).resolve()
        if root not in target.parents and target != root:
            raise ConflictError("Invalid local sandbox destination")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)

    def download_file(self, handle: SandboxHandle, *, source: str, destination: Path) -> None:
        root = Path(handle.root_path).resolve()
        candidate = Path(source)
        target = candidate if candidate.is_absolute() else root / "workspace" / "axiom" / candidate
        target = target.resolve()
        if root not in target.parents and target != root:
            raise ConflictError("Invalid local sandbox source")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(target, destination)

    def remove_path(self, handle: SandboxHandle, *, path: str) -> None:
        root = Path(handle.root_path).resolve()
        candidate = Path(path)
        target = (candidate if candidate.is_absolute() else root / "workspace" / "axiom" / candidate).resolve()
        if root not in target.parents or target == root:
            raise ConflictError("Invalid local sandbox cleanup path")
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink(missing_ok=True)

    @staticmethod
    def _resolve_worker_path(handle: SandboxHandle, value: str) -> Path:
        root = Path(handle.root_path).resolve() / "workspace" / "axiom"
        candidate = Path(value)
        target = (candidate if candidate.is_absolute() else root / candidate).resolve()
        if target != root and root not in target.parents:
            raise ConflictError("Invalid local sandbox file path")
        return target

    def read_file(self, handle: SandboxHandle, *, source: str) -> bytes:
        return self._resolve_worker_path(handle, source).read_bytes()

    def write_file(self, handle: SandboxHandle, *, destination: str, content: bytes) -> None:
        target = self._resolve_worker_path(handle, destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    def append_file(self, handle: SandboxHandle, *, destination: str, content: bytes) -> None:
        target = self._resolve_worker_path(handle, destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("ab") as stream:
            stream.write(content)

    def exists(self, handle: SandboxHandle, *, path: str) -> bool:
        return self._resolve_worker_path(handle, path).exists()

    def download_directory(self, handle: SandboxHandle, *, source: str, destination: Path) -> None:
        source_path = self._resolve_worker_path(handle, source)
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(source_path, destination)

    def open_pty(self, handle: SandboxHandle, *, cwd: str, env: dict[str, str], timeout: float):
        raise RuntimeError("LocalExecutionBackend does not expose a remote PTY")

    def pty_send_stdin(self, pty, data: bytes) -> None:
        raise RuntimeError("LocalExecutionBackend does not expose a remote PTY")

    def pty_wait(self, pty, on_data):
        raise RuntimeError("LocalExecutionBackend does not expose a remote PTY")

    def pty_kill(self, pty) -> None:
        raise RuntimeError("LocalExecutionBackend does not expose a remote PTY")


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
    def _sdk():
        """Load the official CubeSandbox SDK lazily.

        The dependency is optional for local development/tests, but production
        Cube mode fails closed when it is missing.
        """
        try:
            from cubesandbox import Config, Sandbox  # type: ignore

            return Config, Sandbox
        except ImportError:
            raise RuntimeError("CubeSandbox backend requires the official cubesandbox SDK")

    def _config(self):
        Config, _ = self._sdk()
        values: dict[str, Any] = {}
        if self.api_key:
            values["api_key"] = self.api_key
        if self.base_url:
            values["api_url"] = self.base_url
        try:
            return Config(**values)
        except TypeError:
            # Keep compatibility with early SDK builds that used base_url.
            if "api_url" in values:
                values["base_url"] = values.pop("api_url")
            return Config(**values)

    def _connect(self, handle: SandboxHandle):
        _, Sandbox = self._sdk()
        config = self._config()
        return Sandbox.connect(handle.sandbox_id, config=config)

    def create(self, *, task_id: str, root: Path, template_version: str, resource_profile: dict, network_policy: dict) -> SandboxHandle:
        _, Sandbox = self._sdk()
        config = self._config()
        sandbox = Sandbox.create(template=self.template, config=config)
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
            metadata={
                "task_id": task_id,
                "template_version": template_version,
                "resource_profile": resource_profile,
                "network_policy": network_policy,
            },
        )

    def destroy(self, handle: SandboxHandle) -> None:
        # A concrete deployment may use Sandbox.connect(id).kill().  Keep this
        # adapter conservative when the optional SDK is absent.
        try:
            sandbox = self._connect(handle)
            kill = getattr(sandbox, "kill", None) or getattr(sandbox, "close", None)
            if kill is not None:
                kill()
        except (ImportError, OSError, RuntimeError):
            return

    def connect(self, handle: SandboxHandle) -> SandboxHandle:
        self._connect(handle)
        return handle

    def healthcheck(self, handle: SandboxHandle, *, timeout: float = 10.0) -> None:
        sandbox = self._connect(handle)
        commands = getattr(sandbox, "commands", None)
        run = getattr(commands, "run", None) if commands is not None else None
        if run is None:
            raise RuntimeError("CubeSandbox SDK does not expose commands.run")
        result = run(
            "test -d /workspace/axiom",
            timeout=max(1.0, float(timeout)),
            cwd="/workspace/axiom",
        )
        exit_code = getattr(result, "exit_code", getattr(result, "returncode", 0))
        if int(exit_code or 0) != 0:
            raise RuntimeError("CubeSandbox workspace healthcheck failed")

    def run_directory(self, handle: SandboxHandle, *, owner_id: str, lease_id: str) -> Path:
        for value in (owner_id, lease_id):
            if not value or "/" in value or "\\" in value or value in {".", ".."}:
                raise ConflictError("Invalid sandbox run owner")
        path = Path(handle.root_path) / "runs" / owner_id / lease_id
        sandbox = self._connect(handle)
        commands = getattr(sandbox, "commands", None)
        run = getattr(commands, "run", None) if commands is not None else None
        if run is None:
            raise RuntimeError("CubeSandbox SDK does not expose commands.run")
        result = run(f"mkdir -p {path}", timeout=10, cwd=handle.root_path)
        exit_code = getattr(result, "exit_code", getattr(result, "returncode", 0))
        if int(exit_code or 0) != 0:
            raise RuntimeError(f"failed to create Cube run directory: {path}")
        return path

    def upload_file(self, handle: SandboxHandle, *, source: Path, destination: str) -> None:
        sandbox = self._connect(handle)
        files = getattr(sandbox, "files", None)
        write = getattr(files, "write", None) if files is not None else None
        if write is None:
            raise RuntimeError("CubeSandbox SDK does not expose files.write")
        self._write_payload(write, self._worker_path(handle, destination), source.read_bytes())

    def download_file(self, handle: SandboxHandle, *, source: str, destination: Path) -> None:
        sandbox = self._connect(handle)
        files = getattr(sandbox, "files", None)
        read = getattr(files, "read", None) if files is not None else None
        if read is None:
            raise RuntimeError("CubeSandbox SDK does not expose files.read")
        payload = self._read_payload(read, self._worker_path(handle, source))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(bytes(payload))

    def remove_path(self, handle: SandboxHandle, *, path: str) -> None:
        sandbox = self._connect(handle)
        files = getattr(sandbox, "files", None)
        remove = getattr(files, "remove", None) if files is not None else None
        if remove is None:
            raise RuntimeError("CubeSandbox SDK does not expose files.remove")
        target = self._worker_path(handle, path)
        if target == "/workspace/axiom":
            raise ConflictError("Cannot remove the Cube sandbox root")
        remove(target)

    @staticmethod
    def _worker_path(handle: SandboxHandle, value: str) -> str:
        root = "/workspace/axiom"
        candidate = str(value or "")
        if not candidate:
            raise ConflictError("Sandbox file path is required")
        if candidate.startswith("/"):
            normalized = candidate
        else:
            normalized = f"{root}/{candidate.lstrip('/')}"
        # POSIX normalization without allowing a path to escape the worker
        # root.  The SDK itself may expose a different path type, so keep
        # this check in the AXIOM adapter rather than relying on the service.
        from posixpath import normpath

        normalized = normpath(normalized)
        if normalized != root and not normalized.startswith(root + "/"):
            raise ConflictError("Invalid Cube sandbox file path")
        return normalized

    def read_file(self, handle: SandboxHandle, *, source: str) -> bytes:
        sandbox = self._connect(handle)
        files = getattr(sandbox, "files", None)
        read = getattr(files, "read", None) if files is not None else None
        if read is None:
            raise RuntimeError("CubeSandbox SDK does not expose files.read")
        return self._read_payload(read, self._worker_path(handle, source))

    def write_file(self, handle: SandboxHandle, *, destination: str, content: bytes) -> None:
        sandbox = self._connect(handle)
        files = getattr(sandbox, "files", None)
        write = getattr(files, "write", None) if files is not None else None
        if write is None:
            raise RuntimeError("CubeSandbox SDK does not expose files.write")
        self._write_payload(write, self._worker_path(handle, destination), content)

    def append_file(self, handle: SandboxHandle, *, destination: str, content: bytes) -> None:
        sandbox = self._connect(handle)
        files = getattr(sandbox, "files", None)
        append = getattr(files, "append", None) if files is not None else None
        if append is not None:
            self._write_payload(append, self._worker_path(handle, destination), content)
            return
        # Older Cube SDKs only expose write/read.  Preserve append semantics
        # with a bounded read-modify-write fallback.
        existing = self.read_file(handle, source=destination) if self.exists(handle, path=destination) else b""
        self.write_file(handle, destination=destination, content=existing + content)

    def exists(self, handle: SandboxHandle, *, path: str) -> bool:
        sandbox = self._connect(handle)
        files = getattr(sandbox, "files", None)
        exists = getattr(files, "exists", None) if files is not None else None
        if exists is not None:
            return bool(exists(self._worker_path(handle, path)))
        try:
            self.read_file(handle, source=path)
        except (FileNotFoundError, OSError):
            return False
        return True

    def download_directory(self, handle: SandboxHandle, *, source: str, destination: Path) -> None:
        sandbox = self._connect(handle)
        files = getattr(sandbox, "files", None)
        list_files = getattr(files, "list", None) if files is not None else None
        if list_files is None:
            raise RuntimeError("CubeSandbox SDK does not expose files.list")
        root = self._worker_path(handle, source)
        destination.mkdir(parents=True, exist_ok=True)

        def copy_directory(remote: str, local: Path) -> None:
            local.mkdir(parents=True, exist_ok=True)
            try:
                entries = list_files(remote, depth=1)
            except TypeError:
                entries = list_files(remote)
            for entry in entries or []:
                if isinstance(entry, dict):
                    name = str(entry.get("name") or "")
                    entry_path = str(entry.get("path") or "")
                    kind = str(entry.get("type") or entry.get("kind") or "file").lower()
                else:
                    name = str(getattr(entry, "name", "") or "")
                    entry_path = str(getattr(entry, "path", "") or "")
                    kind = str(getattr(entry, "type", getattr(entry, "kind", "file"))).lower()
                if not name:
                    name = entry_path.rstrip("/").rsplit("/", 1)[-1]
                if not name or name in {".", ".."} or Path(name).name != name:
                    continue
                remote_path = entry_path or f"{remote.rstrip('/')}/{name}"
                if "dir" in kind:
                    copy_directory(remote_path, local / name)
                else:
                    (local / name).write_bytes(self.read_file(handle, source=remote_path))

        copy_directory(root, destination)

    def open_pty(self, handle: SandboxHandle, *, cwd: str, env: dict[str, str], timeout: float):
        sandbox = self._connect(handle)
        pty_module = getattr(sandbox, "pty", None)
        create = getattr(pty_module, "create", None) if pty_module is not None else None
        if create is None:
            raise RuntimeError("CubeSandbox SDK does not expose pty.create")
        try:
            from cubesandbox import PtySize  # type: ignore

            size = PtySize(rows=40, cols=160)
        except ImportError as exc:
            raise RuntimeError("CubeSandbox SDK does not expose PtySize") from exc
        return create(size, cwd=cwd, envs=env, timeout=max(1.0, timeout))

    def pty_send_stdin(self, pty, data: bytes) -> None:
        send = getattr(pty, "send_stdin", None)
        if send is None:
            raise RuntimeError("CubeSandbox PTY does not expose send_stdin")
        send(data)

    def pty_wait(self, pty, on_data):
        wait = getattr(pty, "wait", None)
        if wait is None:
            raise RuntimeError("CubeSandbox PTY does not expose wait")
        return wait(on_data=on_data)

    def pty_kill(self, pty) -> None:
        kill = getattr(pty, "kill", None)
        if kill is not None:
            kill()

    @staticmethod
    def _read_payload(read, path: str) -> bytes:
        payload = read(path)
        if isinstance(payload, bytes):
            return payload
        if isinstance(payload, str):
            return payload.encode("utf-8")
        for attribute in ("content", "data", "body"):
            value = getattr(payload, attribute, None)
            if value is not None:
                if isinstance(value, str):
                    return value.encode("utf-8")
                return bytes(value)
        return bytes(payload)

    @staticmethod
    def _write_payload(write, path: str, content: bytes) -> None:
        try:
            write(path, content)
        except TypeError:
            # A few early SDK builds accepted text only.  This fallback keeps
            # text logs usable while binary artifacts still fail closed rather
            # than being silently re-encoded.
            try:
                write(path, content.decode("utf-8"))
            except UnicodeDecodeError:
                raise
