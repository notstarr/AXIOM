from __future__ import annotations

from types import SimpleNamespace

from axiom.dispatcher.v2.loop import V2DispatcherLoop
from axiom.dispatcher.v2.schemas import DispatcherSnapshot, IntentSnapshot, TaskSnapshot, WorkerSnapshot
from axiom.modules.settings.schemas import RuntimeConfig


class FakeClient:
    def __init__(self, snapshot):
        self.value = snapshot
        self.closed = False

    def runtime_settings(self):
        return SimpleNamespace(
            config=RuntimeConfig(
                max_workers=2,
                max_running_tasks=1,
                max_task_workers=1,
                bootstrap_limit=1,
                reason_limit=1,
                explore_limit=1,
            )
        )

    def snapshot(self):
        return self.value

    def recover_stale_leases(self, timeout_seconds):
        return {"intents_released": 0, "reasons_released": 0}

    def close(self):
        self.closed = True


class RecordingRunners:
    def __init__(self):
        self.calls = []

    def run(self, action, worker, runtime):
        self.calls.append((action.kind, action.task_id, action.intent_id, worker.name))


def test_dispatch_loop_reconciles_and_obeys_task_concurrency(tmp_path) -> None:
    snapshot = DispatcherSnapshot(
        tasks=[
            TaskSnapshot(
                id="t_001",
                status="running",
                phase="explore",
                intents=[
                    IntentSnapshot(id="i002", status="pending", creator="reason"),
                    IntentSnapshot(id="i003", status="pending", creator="reason"),
                ],
            )
        ],
        workers=[
            WorkerSnapshot(
                name="worker-a",
                capabilities=["explore", "reason", "bootstrap"],
                max_running=2,
                running=0,
                priority=0,
                config={
                    "env": {
                        "PI_MODEL": "test",
                        "PI_BASE_URL": "http://example.invalid",
                        "PI_PROVIDER_API": "openai-completions",
                    }
                },
            )
        ],
    )
    client = FakeClient(snapshot)
    runners = RecordingRunners()
    loop = V2DispatcherLoop(
        server="http://unused",
        service_token="token",
        client=client,
        runners=runners,
    )
    loop.run(once=True)

    assert len(runners.calls) == 1
    assert runners.calls[0][:3] == ("explore", "t_001", "i002")
    assert client.closed is True
