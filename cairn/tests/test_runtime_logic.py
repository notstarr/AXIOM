from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from axiom.dispatcher.protocol.client import ApiResult
from axiom.dispatcher.runtime.cancellation import TaskCancellation
from axiom.dispatcher.runtime.heartbeat import HeartbeatLease
from axiom.dispatcher.runtime.process import ManagedProcess


@dataclass
class FakeProcess:
    cancelled: list[str] = field(default_factory=list)
    kill_count: int = 0

    def cancel(self, reason: str) -> None:
        self.cancelled.append(reason)

    def kill(self) -> None:
        self.kill_count += 1


def test_task_cancellation_keeps_first_reason_and_cancels_late_process() -> None:
    cancellation = TaskCancellation()

    assert cancellation.cancel("project stopped")
    assert not cancellation.cancel("second reason")
    assert cancellation.reason == "project stopped"

    process = FakeProcess()
    cancellation.attach_process(process)  # type: ignore[arg-type]
    assert process.cancelled == ["project stopped"]


def test_heartbeat_conflict_failure_kills_attached_process() -> None:
    process = FakeProcess()
    lease = HeartbeatLease(lambda: ApiResult(409, text="lost"), "intent", "worker", interval=60)
    lease.attach_process(process)  # type: ignore[arg-type]

    lease._fail(409, "lost")

    assert lease.failure is not None
    assert lease.failure.status_code == 409
    assert process.kill_count == 1


def test_managed_process_builds_with_env_and_command(tmp_path: Path) -> None:
    process = ManagedProcess(["agent", "-p", "prompt"], {"A": "B"}, cwd=str(tmp_path))

    assert process.command == ["agent", "-p", "prompt"]
    assert process.env == {"A": "B"}
    assert process.cwd == str(tmp_path)


def test_write_graph_snapshot_creates_file(tmp_path: Path, monkeypatch) -> None:
    from axiom.dispatcher.tasks.common import write_graph_snapshot_file
    import uuid

    import axiom.dispatcher.tasks.common as common

    test_dir = tmp_path / "axiom-prompts"
    test_dir.mkdir(parents=True)

    monkeypatch.setattr(common, "GRAPH_SNAPSHOT_ROOT", str(test_dir))
    monkeypatch.setattr(uuid, "uuid4", lambda: type("UUID", (), {"hex": "abc123"})())

    result = write_graph_snapshot_file("proj-001", "facts: []\n", phase="test-phase")

    graph_file = test_dir / "test-phase-abc123" / "graph.yaml"
    assert graph_file.exists()
    assert graph_file.read_text(encoding="utf-8") == "facts: []\n"
    assert str(graph_file) in result
