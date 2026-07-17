from __future__ import annotations

from pathlib import Path

from axiom.dispatcher.runtime.process import ProcessResult
from axiom.dispatcher.v2.runners import PhaseRunners
from axiom.dispatcher.v2.schemas import DispatchAction, WorkerSnapshot
from axiom.dispatcher.workers.base import DriverResult
from axiom.modules.graph.schemas import Intent
from axiom.modules.reason_workspace.schemas import ReasonWorkspace, WorkspaceDraft
from axiom.modules.settings.schemas import RuntimeConfig
from axiom.modules.tasks.schemas import Origin, Task, TaskScope


class ShellDriver:
    def prepare_session(self):
        return None

    def build_execute(self, worker, prompt, session, phase=""):
        return DriverResult(argv=["/bin/sh", "-c", "printf 'phase summary'"], session=None)

    def build_conclude(self, worker, prompt, session, phase=""):
        raise AssertionError("no session should skip conclude")

    def extract_session(self, session, stdout, stderr):
        return None

    def extract_response_text(self, stdout, stderr):
        return stdout


class FakeRunnerClient:
    base_url = "http://127.0.0.1:8889"
    service_token = "token"

    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.concluded = []
        self.released = []
        self.cooldowns = []
        self.reason_released = []
        self.task_value = Task(
            id="t_001",
            project_id="prj_001",
            title="Task",
            origin=Origin(host="example.com", original_input="example.com"),
            goal="finish",
            status="running",
            phase="bootstrap",
            scope=TaskScope(enabled=True, targets=["example.com"]),
            guard_enabled=False,
            external_report_enabled=False,
            worker_pool=[],
            legacy_readonly=False,
            created_at="2026-07-17T00:00:00Z",
            updated_at="2026-07-17T00:00:00Z",
        )
        self.intent = Intent(
            id="i001",
            task_id="t_001",
            from_facts=["origin"],
            description="bootstrap",
            status="pending",
            creator="dispatcher.bootstrap",
            targets=["example.com"],
            created_at="2026-07-17T00:00:00Z",
        )

    def task(self, task_id):
        return self.task_value

    def intents(self, task_id):
        return [self.intent]

    def claim_intent(self, *args):
        return self.intent

    def heartbeat_intent(self, *args):
        return self.intent

    def build_context(self, task_id, attempt_id):
        manifest = self.tmp_path / "data" / "contexts" / "manifests" / f"{attempt_id}.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text("{}")
        return {"manifest_path": str(manifest), "revision": 1, "resources": {}}

    def conclude_intent(self, task_id, intent_id, worker, description):
        self.concluded.append((task_id, intent_id, worker, description))
        return {}

    def release_intent(self, *args, **kwargs):
        self.released.append((args, kwargs))
        return self.intent

    def set_worker_cooldown(self, *args, **kwargs):
        self.cooldowns.append((args, kwargs))

    def audit(self, body):
        pass


def worker_snapshot():
    return WorkerSnapshot(
        name="worker-a",
        capabilities=["bootstrap", "reason", "explore"],
        max_running=1,
        running=0,
        priority=0,
        config={
            "env": {
                "PI_MODEL": "test-model",
                "PI_BASE_URL": "http://example.invalid",
                "PI_PROVIDER_API": "openai-completions",
            }
        },
    )


def test_bootstrap_runner_claims_executes_and_concludes(monkeypatch, tmp_path) -> None:
    import axiom.dispatcher.v2.runners as runners_module

    monkeypatch.setattr(runners_module, "get_driver", lambda _name: ShellDriver())
    client = FakeRunnerClient(tmp_path)
    runner = PhaseRunners(client)
    runner.run(
        DispatchAction(kind="bootstrap", task_id="t_001", intent_id="i001"),
        worker_snapshot(),
        RuntimeConfig(heartbeat_interval_seconds=1),
    )

    assert client.released == []
    assert client.concluded == [("t_001", "i001", "worker-a", "phase summary")]
    run_root = tmp_path / "data" / "runs" / "t_001" / "i001"
    assert any(path.is_dir() for path in run_root.iterdir())
