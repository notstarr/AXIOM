from __future__ import annotations

import pytest

from axiom.modules.sandboxes.backend import CubeSandboxExecutionBackend
from axiom.modules.sandboxes.schemas import SandboxHandle
from axiom.shared.errors import ConflictError


def test_cube_backend_rejects_paths_outside_worker_root() -> None:
    handle = SandboxHandle(sandbox_id="cube-1", root_path="/workspace/axiom", backend="cube")
    backend = CubeSandboxExecutionBackend()

    assert backend._worker_path(handle, "runs/i1/l1/output.txt") == (
        "/workspace/axiom/runs/i1/l1/output.txt"
    )
    with pytest.raises(ConflictError):
        backend._worker_path(handle, "../../etc/passwd")
    with pytest.raises(ConflictError):
        backend._worker_path(handle, "/tmp/host-file")


def test_cube_backend_does_not_remove_worker_root() -> None:
    handle = SandboxHandle(sandbox_id="cube-1", root_path="/workspace/axiom", backend="cube")
    backend = CubeSandboxExecutionBackend()
    with pytest.raises(ConflictError):
        backend._worker_path(handle, "/workspace/axiom/../../")


def test_cube_payload_adapters_support_sdk_objects() -> None:
    class Payload:
        content = b"hello"

    assert CubeSandboxExecutionBackend._read_payload(lambda _path: Payload(), "x") == b"hello"

    writes: list[object] = []

    def write(_path: str, value: object) -> None:
        writes.append(value)
        if not isinstance(value, str):
            raise TypeError("text only")

    CubeSandboxExecutionBackend._write_payload(write, "/workspace/axiom/a.txt", b"hello")
    assert writes == [b"hello", "hello"]
