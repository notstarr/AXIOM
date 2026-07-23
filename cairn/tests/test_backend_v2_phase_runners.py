from __future__ import annotations

from pathlib import Path
import json
from types import SimpleNamespace

import pytest

from axiom.dispatcher.runtime.process import ProcessResult
from axiom.dispatcher.runtime.rpc import RpcProcessResult
from axiom.dispatcher.v2.client import V2ProtocolError
from axiom.dispatcher.v2.runners import PhaseRunners, RunnerFailure
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
        command = (
            'mkdir -p "$AXIOM_RUN_DIR"; '
            'printf inventory > "$AXIOM_RUN_DIR/asset_inventory.md"; '
            'printf leads > "$AXIOM_RUN_DIR/initial_leads.md"; '
            'printf findings > "$AXIOM_RUN_DIR/findings.md"; '
            "printf 'phase summary'"
        )
        return DriverResult(argv=["/bin/sh", "-c", command], session=None)

    def build_conclude(self, worker, prompt, session, phase=""):
        raise AssertionError("no session should skip conclude")

    def extract_session(self, session, stdout, stderr):
        return None

    def extract_response_text(self, stdout, stderr):
        return stdout


class UsageShellDriver(ShellDriver):
    def build_execute(self, worker, prompt, session, phase=""):
        payload = (
            '{"type":"agent_end","messages":['
            '{"role":"assistant","usage":{"input_tokens":123,"output_tokens":45,"total_tokens":168}}'
            "]}"
        )
        command = (
            'mkdir -p "$AXIOM_RUN_DIR"; '
            'printf inventory > "$AXIOM_RUN_DIR/asset_inventory.md"; '
            'printf leads > "$AXIOM_RUN_DIR/initial_leads.md"; '
            'printf findings > "$AXIOM_RUN_DIR/findings.md"; '
            f"printf '%s\\n' '{payload}'"
        )
        return DriverResult(argv=["/bin/sh", "-c", command], session=None)

    def extract_response_text(self, stdout, stderr):
        return "phase summary"


class ArtifactShellDriver(ShellDriver):
    def build_execute(self, worker, prompt, session, phase=""):
        command = (
            'mkdir -p "$AXIOM_RUN_DIR"; '
            'printf inventory > "$AXIOM_RUN_DIR/asset_inventory.md"; '
            'printf leads > "$AXIOM_RUN_DIR/initial_leads.md"; '
            'printf findings > "$AXIOM_RUN_DIR/findings.md"; '
            "printf 'phase summary'"
        )
        return DriverResult(argv=["/bin/sh", "-c", command], session=None)


class FakeRunnerClient:
    base_url = "http://127.0.0.1:8889"
    service_token = "token"

    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.concluded = []
        self.released = []
        self.cooldowns = []
        self.reason_released = []
        self.audit_events = []
        self.artifacts = []
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

    def build_context(self, task_id, attempt_id, **_kwargs):
        manifest = self.tmp_path / "data" / "contexts" / "manifests" / f"{attempt_id}.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text("{}")
        return {"manifest_path": str(manifest), "revision": 1, "resources": {}}

    def conclude_intent(self, task_id, intent_id, worker, description, *, summary_source="legacy"):
        self.concluded.append((task_id, intent_id, worker, description, summary_source))
        return {}

    def register_artifact(self, task_id, **payload):
        existing = next(
            (item for item in self.artifacts if item[1].get("path") == payload.get("path")),
            None,
        )
        if existing is not None:
            existing[1].update(payload)
            payload = existing[1]
        else:
            payload["id"] = f"a{len(self.artifacts) + 1:03d}"
            self.artifacts.append((task_id, payload))
        return payload

    def release_intent(self, *args, **kwargs):
        self.released.append((args, kwargs))
        return self.intent

    def set_worker_cooldown(self, *args, **kwargs):
        self.cooldowns.append((args, kwargs))

    def audit(self, body):
        self.audit_events.append(body)


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


def explore_client(tmp_path):
    client = FakeRunnerClient(tmp_path)
    client.task_value = client.task_value.model_copy(update={"phase": "explore"})
    client.intent = client.intent.model_copy(
        update={
            "id": "i002",
            "from_facts": ["f001"],
            "description": "验证接口访问控制",
            "creator": "reason",
        }
    )
    return client


def write_explore_artifacts(worker):
    run_dir = Path(worker.env["AXIOM_RUN_DIR"])
    (run_dir / "findings.md").write_text("# 已确认结果\n无新增漏洞", encoding="utf-8")
    (run_dir / "leads.md").write_text("# 未验证线索\n无新增线索", encoding="utf-8")


@pytest.mark.parametrize(
    ("exc", "expected_kind", "expected_class"),
    [
        (V2ProtocolError("connection reset"), "v2protocolerror", "transient"),
        (RunnerFailure("provider process exited"), "runnerfailure", "transient"),
        (RunnerFailure("lease heartbeat failed: connection reset"), "runnerfailure", "transient"),
        (RunnerFailure("explore execute budget exhausted"), "timeout", "execution"),
        (
            RunnerFailure("explore required artifacts missing after conclusion: findings"),
            "artifact_contract",
            "execution",
        ),
    ],
)
def test_runner_failure_classification(exc, expected_kind, expected_class) -> None:
    kind = PhaseRunners._failure_kind(exc)
    assert kind == expected_kind
    assert PhaseRunners._failure_class(kind, exc) == expected_class


def test_explore_uses_only_validated_execute_summary(monkeypatch, tmp_path) -> None:
    import axiom.dispatcher.v2.runners as runners_module

    class ExploreDriver(ShellDriver):
        def build_execute(self, worker, prompt, session, phase=""):
            return DriverResult(argv=["/bin/true"], session=None)

    monkeypatch.setattr(runners_module, "get_driver", lambda _name: ExploreDriver())
    client = explore_client(tmp_path)
    runner = PhaseRunners(client)

    def execute(worker, **_kwargs):
        write_explore_artifacts(worker)
        return (
            ProcessResult(0, "", ""),
            None,
            '{"accepted":true,"data":{"description":"本轮验证 example.com 的认证与访问控制，匿名请求均被拒绝，未确认可利用漏洞；后续需使用不同角色账号复核对象级授权。详见 findings.md，未验证方向见 leads.md。"}}',
        )

    monkeypatch.setattr(runner, "_execute", execute)
    runner.run(
        DispatchAction(kind="explore", task_id="t_001", intent_id="i002"),
        worker_snapshot(),
        RuntimeConfig(heartbeat_interval_seconds=1),
    )

    assert client.concluded[0][3].startswith("本轮验证 example.com")
    assert client.concluded[0][4] == "explore_execute_summary"


def test_explore_summary_ignores_think_text_outside_json() -> None:
    description = PhaseRunners._validated_explore_summary(
        '<think>内部推理不应进入图谱</think>\n'
        '{"accepted":true,"data":{"description":"本轮验证 example.com 的认证与访问控制，匿名请求均被拒绝，未确认可利用漏洞；后续需使用不同角色账号复核对象级授权。详见 findings.md，未验证方向见 leads.md。"}}'
    )

    assert description.startswith("本轮验证 example.com")
    assert "think" not in description


@pytest.mark.parametrize(
    "description",
    (
        "本次 Intent 的确认结果见 findings.md，未验证线索见 leads.md",
        "所有测试结果详见 findings.md，后续方向详见 leads.md",
        "已完成收口，请查看 findings.md 和 leads.md 获取完整内容",
    ),
)
def test_explore_summary_rejects_artifact_only_pointer(description: str) -> None:
    response = json.dumps(
        {"accepted": True, "data": {"description": description}},
        ensure_ascii=False,
    )

    with pytest.raises(RunnerFailure, match="only an artifact pointer"):
        PhaseRunners._validated_explore_summary(response)


def test_explore_plain_execute_response_enters_structured_conclude(monkeypatch, tmp_path) -> None:
    import axiom.dispatcher.v2.runners as runners_module

    monkeypatch.setattr(runners_module, "get_driver", lambda _name: ShellDriver())
    client = explore_client(tmp_path)
    runner = PhaseRunners(client)

    def execute(worker, **_kwargs):
        write_explore_artifacts(worker)
        return ProcessResult(0, "", ""), "session-1", "phase summary"

    def conclude(worker, **_kwargs):
        return (
            ProcessResult(0, "", ""),
            '{"accepted":true,"data":{"description":"收口确认结果见 findings.md，后续线索见 leads.md"}}',
        )

    monkeypatch.setattr(runner, "_execute", execute)
    monkeypatch.setattr(runner, "_conclude", conclude)
    runner.run(
        DispatchAction(kind="explore", task_id="t_001", intent_id="i002"),
        worker_snapshot(),
        RuntimeConfig(heartbeat_interval_seconds=1),
    )

    assert "findings.md" in client.concluded[0][3]
    assert "leads.md" in client.concluded[0][3]
    assert "phase summary" not in client.concluded[0][3]
    assert client.concluded[0][4] == "explore_artifact_summary"
    assert any(item["event_type"] == "explore.summary_invalid" for item in client.audit_events)


def test_explore_never_turns_stop_ack_into_fact(monkeypatch, tmp_path) -> None:
    import axiom.dispatcher.v2.runners as runners_module

    monkeypatch.setattr(runners_module, "get_driver", lambda _name: ShellDriver())
    client = explore_client(tmp_path)
    runner = PhaseRunners(client)

    def execute(worker, **_kwargs):
        write_explore_artifacts(worker)
        return ProcessResult(0, "", ""), "session-1", "phase summary"

    monkeypatch.setattr(runner, "_execute", execute)
    monkeypatch.setattr(
        runner,
        "_conclude",
        lambda *_args, **_kwargs: (ProcessResult(0, "", ""), "已停止执行，等待 Conclude"),
    )

    runner.run(
        DispatchAction(kind="explore", task_id="t_001", intent_id="i002"),
        worker_snapshot(),
        RuntimeConfig(heartbeat_interval_seconds=1),
    )
    assert client.concluded[0][4] == "explore_artifact_summary"
    assert "已停止执行" not in client.concluded[0][3]
    assert "findings.md" in client.concluded[0][3]


def test_explore_conclude_timeout_never_uses_execute_response_as_fact(monkeypatch, tmp_path) -> None:
    import axiom.dispatcher.v2.runners as runners_module

    monkeypatch.setattr(runners_module, "get_driver", lambda _name: ShellDriver())
    client = explore_client(tmp_path)
    runner = PhaseRunners(client)

    def execute(worker, **_kwargs):
        write_explore_artifacts(worker)
        return ProcessResult(0, "", ""), "session-1", "phase summary"

    monkeypatch.setattr(runner, "_execute", execute)
    monkeypatch.setattr(
        runner,
        "_conclude",
        lambda *_args, **_kwargs: (
            ProcessResult(137, "", "", timed_out=True),
            "",
        ),
    )

    runner.run(
        DispatchAction(kind="explore", task_id="t_001", intent_id="i002"),
        worker_snapshot(),
        RuntimeConfig(heartbeat_interval_seconds=1),
    )
    assert client.concluded[0][4] == "explore_artifact_summary"
    assert "phase summary" not in client.concluded[0][3]


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
    assert len(client.concluded) == 1
    description = client.concluded[0][3]
    assert "攻击面覆盖 [a001]: inventory" in description
    assert "重要线索 [a002]: leads" in description
    assert "确认结果 [a003]: findings" in description
    assert "产物索引：a001=asset_inventory" in description
    assert client.concluded[0][4] == "bootstrap_artifact_summary"
    run_root = tmp_path / "data" / "runs" / "t_001" / "i001"
    assert any(path.is_dir() for path in run_root.iterdir())


def test_runner_removes_private_credential_files(tmp_path) -> None:
    run_dir = tmp_path / "run"
    nested = run_dir / "nested"
    nested.mkdir(parents=True)
    secret = nested / "credential-cred_001.json"
    keep = nested / "evidence.json"
    secret.write_text('{"token":"secret"}', encoding="utf-8")
    keep.write_text('{"status":"ok"}', encoding="utf-8")

    PhaseRunners._cleanup_private_credential_files(run_dir)

    assert not secret.exists()
    assert keep.exists()


def test_runner_redacts_pi_session_and_compact_evidence_material(tmp_path) -> None:
    run_dir = tmp_path / "run"
    session_dir = run_dir / ".pi-session"
    session_dir.mkdir(parents=True)
    jwt = "eyJ" + "a" * 40 + "." + "b" * 40 + "." + "c" * 20
    session_path = session_dir / "session.jsonl"
    session_path.write_text(
        json.dumps(
            {
                "type": "message",
                "message": {
                    "role": "toolResult",
                    "content": [{"type": "text", "text": f"access_token={jwt}"}],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    evidence_path = run_dir / "execute_evidence.md"
    evidence_path.write_text(
        f"Authorization: Bearer {jwt}\n",
        encoding="utf-8",
    )

    PhaseRunners._redact_session_material(run_dir)

    session_content = session_path.read_text(encoding="utf-8")
    evidence_content = evidence_path.read_text(encoding="utf-8")
    assert jwt not in session_content
    assert jwt not in evidence_content
    assert "[REDACTED]" in session_content
    assert "[REDACTED]" in evidence_content
    # Redaction must preserve valid JSONL so Pi can resume the session safely.
    json.loads(session_content)


def test_runner_starts_lease_heartbeat_before_context_initialization(monkeypatch, tmp_path) -> None:
    import axiom.dispatcher.v2.runners as runners_module

    monkeypatch.setattr(runners_module, "get_driver", lambda _name: ShellDriver())
    client = FakeRunnerClient(tmp_path)
    events = []
    original_context = client.build_context
    original_heartbeat = client.heartbeat_intent

    def record_context(*args, **kwargs):
        events.append("context")
        return original_context(*args, **kwargs)

    def record_heartbeat(*args, **kwargs):
        events.append("heartbeat")
        return original_heartbeat(*args, **kwargs)

    client.build_context = record_context
    client.heartbeat_intent = record_heartbeat

    PhaseRunners(client).run(
        DispatchAction(kind="bootstrap", task_id="t_001", intent_id="i001"),
        worker_snapshot(),
        RuntimeConfig(heartbeat_interval_seconds=60),
    )

    assert events
    assert events.index("heartbeat") < events.index("context")


def test_rpc_runner_steers_live_session_and_concludes(monkeypatch, tmp_path) -> None:
    import axiom.dispatcher.v2.runners as runners_module

    class RpcDriver(ShellDriver):
        def supports_rpc(self):
            return True

        def prepare_session(self):
            return "rpc-session"

        def build_rpc(self, _worker, _session, phase=""):
            return ["/bin/true"]

    class FakeRpc:
        def __init__(self, _command, env):
            self.env = env

        def start(self):
            pass

        def prompt(self, _message, **_kwargs):
            run_dir = Path(self.env["AXIOM_RUN_DIR"])
            for filename in ("asset_inventory.md", "initial_leads.md", "findings.md"):
                (run_dir / filename).write_text(filename, encoding="utf-8")
            return RpcProcessResult(0, "rpc summary", "", soft_steered=True)

        def stop(self):
            pass

        def kill(self):
            pass

    monkeypatch.setattr(runners_module, "get_driver", lambda _name: RpcDriver())
    monkeypatch.setattr(runners_module, "ManagedRpcProcess", FakeRpc)
    client = FakeRunnerClient(tmp_path)
    events = []
    original_context = client.build_context
    original_heartbeat = client.heartbeat_intent

    def record_context(*args, **kwargs):
        events.append("context")
        return original_context(*args, **kwargs)

    def record_heartbeat(*args, **kwargs):
        events.append("heartbeat")
        return original_heartbeat(*args, **kwargs)

    client.build_context = record_context
    client.heartbeat_intent = record_heartbeat
    runner = PhaseRunners(client)

    runner.run(
        DispatchAction(kind="bootstrap", task_id="t_001", intent_id="i001"),
        worker_snapshot(),
        RuntimeConfig(heartbeat_interval_seconds=1),
    )

    assert client.concluded
    assert events.index("heartbeat") < events.index("context")
    completed = next(item for item in client.audit_events if item["event_type"] == "bootstrap.completed")
    assert completed["payload"]["rpc"] is True
    assert completed["payload"]["soft_steered"] is True


def test_rpc_explore_runner_uses_explore_artifact_contract(monkeypatch, tmp_path) -> None:
    import axiom.dispatcher.v2.runners as runners_module

    class RpcDriver(ShellDriver):
        def supports_rpc(self):
            return True

        def prepare_session(self):
            return "rpc-session"

        def build_rpc(self, _worker, _session, phase=""):
            return ["/bin/true"]

    class FakeRpc:
        def __init__(self, _command, env):
            self.env = env

        def start(self):
            pass

        def prompt(self, _message, **_kwargs):
            run_dir = Path(self.env["AXIOM_RUN_DIR"])
            for filename in ("findings.md", "leads.md"):
                (run_dir / filename).write_text(filename, encoding="utf-8")
            return RpcProcessResult(
                0,
                '{"accepted":true,"data":{"description":"本轮验证 example.com 的认证与访问控制，匿名请求均被拒绝，未确认可利用漏洞；后续需使用不同角色账号复核对象级授权。详见 findings.md，未验证方向见 leads.md。"}}',
                "",
            )

        def stop(self):
            pass

        def kill(self):
            pass

    monkeypatch.setattr(runners_module, "get_driver", lambda _name: RpcDriver())
    monkeypatch.setattr(runners_module, "ManagedRpcProcess", FakeRpc)
    client = FakeRunnerClient(tmp_path)
    runner = PhaseRunners(client)

    runner.run(
        DispatchAction(kind="explore", task_id="t_001", intent_id="i001"),
        worker_snapshot(),
        RuntimeConfig(heartbeat_interval_seconds=1),
    )

    assert [item[1]["artifact_type"] for item in client.artifacts] == ["findings", "leads"]
    completed = next(item for item in client.audit_events if item["event_type"] == "explore.completed")
    assert completed["payload"]["rpc"] is True


def test_rpc_explore_stopped_task_never_enters_conclude_or_retry(monkeypatch, tmp_path) -> None:
    import axiom.dispatcher.v2.runners as runners_module

    class RpcDriver(ShellDriver):
        def supports_rpc(self):
            return True

        def prepare_session(self):
            return "rpc-session"

        def build_rpc(self, _worker, _session, phase=""):
            return ["/bin/true"]

        def build_conclude(self, *_args, **_kwargs):
            raise AssertionError("stopped Task must not enter Conclude")

    client = explore_client(tmp_path)

    class FakeRpc:
        def __init__(self, _command, _env):
            pass

        def start(self):
            pass

        def prompt(self, _message, **_kwargs):
            client.task_value = client.task_value.model_copy(update={"status": "stopped"})
            return RpcProcessResult(0, "stopped", "")

        def stop(self):
            pass

    monkeypatch.setattr(runners_module, "get_driver", lambda _name: RpcDriver())
    monkeypatch.setattr(runners_module, "ManagedRpcProcess", FakeRpc)
    runner = PhaseRunners(client)
    runner.run(
        DispatchAction(kind="explore", task_id="t_001", intent_id="i002"),
        worker_snapshot(),
        RuntimeConfig(heartbeat_interval_seconds=1),
    )

    assert client.concluded == []
    assert client.released == []


def test_rpc_explore_repairs_only_final_json_in_same_session(monkeypatch, tmp_path) -> None:
    import axiom.dispatcher.v2.runners as runners_module

    class RpcDriver(ShellDriver):
        def supports_rpc(self):
            return True

        def prepare_session(self):
            return "rpc-session"

        def build_rpc(self, _worker, _session, phase=""):
            return ["/bin/true"]

    class FakeRpc:
        follow_up_calls = 0

        def __init__(self, _command, env):
            self.env = env

        def start(self):
            pass

        def prompt(self, _message, **_kwargs):
            write_explore_artifacts(SimpleNamespace(env=self.env))
            return RpcProcessResult(0, "测试完成，但不是 JSON", "")

        def follow_up(self, message, **_kwargs):
            type(self).follow_up_calls += 1
            assert "不要继续测试" in message
            return RpcProcessResult(
                0,
                '{"accepted":true,"data":{"description":"本轮验证 example.com 的认证与访问控制，匿名请求均被拒绝，未确认可利用漏洞；后续需使用不同角色账号复核对象级授权。详见 findings.md，未验证方向见 leads.md。"}}',
                "",
            )

        def stop(self):
            pass

    monkeypatch.setattr(runners_module, "get_driver", lambda _name: RpcDriver())
    monkeypatch.setattr(runners_module, "ManagedRpcProcess", FakeRpc)
    client = explore_client(tmp_path)
    runner = PhaseRunners(client)
    monkeypatch.setattr(
        runner,
        "_conclude",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("valid same-session repair must not start independent Conclude")
        ),
    )

    runner.run(
        DispatchAction(kind="explore", task_id="t_001", intent_id="i002"),
        worker_snapshot(),
        RuntimeConfig(heartbeat_interval_seconds=1),
    )

    assert FakeRpc.follow_up_calls == 1
    assert client.concluded[0][4] == "explore_execute_summary"


def test_rpc_explore_does_not_accept_placeholder_artifacts(monkeypatch, tmp_path) -> None:
    import axiom.dispatcher.v2.runners as runners_module

    class RpcDriver(ShellDriver):
        def supports_rpc(self):
            return True

        def prepare_session(self):
            return "rpc-session"

        def build_rpc(self, _worker, _session, phase=""):
            return ["/bin/true"]

    class FakeRpc:
        def __init__(self, _command, env):
            self.env = env

        def start(self):
            pass

        def prompt(self, _message, **_kwargs):
            run_dir = Path(self.env["AXIOM_RUN_DIR"])
            (run_dir / "findings.md").write_text(
                "# Findings\n\n## 测试进行中...\n", encoding="utf-8"
            )
            (run_dir / "leads.md").write_text(
                "# Leads\n\n## 待验证\n需要进一步测试。\n", encoding="utf-8"
            )
            return RpcProcessResult(0, "not json", "")

        def follow_up(self, _message, **_kwargs):
            raise AssertionError("placeholder artifacts must enter independent Conclude")

        def stop(self):
            pass

    monkeypatch.setattr(runners_module, "get_driver", lambda _name: RpcDriver())
    monkeypatch.setattr(runners_module, "ManagedRpcProcess", FakeRpc)
    client = explore_client(tmp_path)
    runner = PhaseRunners(client)
    conclude_calls = []

    def conclude(worker, **kwargs):
        conclude_calls.append(kwargs)
        run_dir = Path(worker.env["AXIOM_RUN_DIR"])
        (run_dir / "findings.md").write_text(
            "# 已确认结果\n\n本轮未确认新增漏洞。证据与结论已整理。",
            encoding="utf-8",
        )
        (run_dir / "leads.md").write_text(
            "# 后续线索\n\n当前无新增线索，建议后续复核认证边界。",
            encoding="utf-8",
        )
        return (
            ProcessResult(0, "", ""),
            '{"accepted":true,"data":{"description":"本轮验证 example.com 的认证与访问控制，匿名请求均被拒绝，未确认可利用漏洞；后续需使用不同角色账号复核对象级授权。详见 findings.md，未验证方向见 leads.md。"}}',
        )

    monkeypatch.setattr(runner, "_conclude", conclude)
    runner.run(
        DispatchAction(kind="explore", task_id="t_001", intent_id="i002"),
        worker_snapshot(),
        RuntimeConfig(heartbeat_interval_seconds=1, explore_conclude_seconds=90),
    )

    assert len(conclude_calls) == 1
    assert client.released == []
    assert client.concluded[0][4] == "explore_conclude_summary"


def test_artifact_fallback_does_not_treat_normal_unverified_leads_as_partial(
    tmp_path,
) -> None:
    client = explore_client(tmp_path)
    runner = PhaseRunners(client)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "findings.md").write_text(
        "# 已确认结果\n\n本轮确认服务端要求认证，未新增漏洞。\n",
        encoding="utf-8",
    )
    (run_dir / "leads.md").write_text(
        "# 后续线索\n\n## 待验证方向\n后续使用授权账号复核角色边界。\n",
        encoding="utf-8",
    )

    summary = runner._artifact_fallback_summary(
        task=client.task_value,
        task_id="t_001",
        intent_id="i002",
        worker="worker-a",
        run_dir=run_dir,
        registered_artifacts={},
        reason="malformed final JSON",
    )

    assert "未形成完整最终结论" not in summary
    assert "严格依据已落盘产物生成" in summary


@pytest.mark.parametrize(
    "content",
    (
        "# Findings\n\n（待填充完整字段分析）\n",
        "# Leads\n\n运行中，线索将逐步追加...\n",
        "# Findings\n\n## 状态\n持续更新\n",
    ),
)
def test_explore_artifact_contract_rejects_unfinished_status_markers(
    tmp_path, content: str
) -> None:
    path = tmp_path / "artifact.md"
    path.write_text(content, encoding="utf-8")

    assert PhaseRunners._explore_artifact_unfinished(path) is True


def test_explore_artifact_contract_allows_normal_runtime_observation(tmp_path) -> None:
    path = tmp_path / "artifact.md"
    path.write_text(
        "# 已确认结果\n\n服务当前运行中，但本轮已完成验证并确认未发现漏洞。\n",
        encoding="utf-8",
    )

    assert PhaseRunners._explore_artifact_unfinished(path) is False


def test_reconcile_redacts_sensitive_artifact_values_before_registration(
    tmp_path,
) -> None:
    client = explore_client(tmp_path)
    runner = PhaseRunners(client)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    findings = run_dir / "findings.md"
    leads = run_dir / "leads.md"
    findings.write_text(
        "# 已确认结果\n\nAuthorization: Bearer raw-secret-value\n"
        "AMAP_KEY=amap-secret-value\n",
        encoding="utf-8",
    )
    leads.write_text(
        "# 后续线索\n\norganization-code: org-secret-value\n",
        encoding="utf-8",
    )

    missing = runner._reconcile_required_artifacts(
        task_id="t_001",
        intent_id="i002",
        phase="explore",
        worker="worker-a",
        run_dir=run_dir,
    )

    assert missing == []
    combined = findings.read_text(encoding="utf-8") + leads.read_text(encoding="utf-8")
    assert "raw-secret-value" not in combined
    assert "amap-secret-value" not in combined
    assert "org-secret-value" not in combined
    assert combined.count("[REDACTED]") == 3


def test_bootstrap_findings_without_structured_vulnerability_are_rejected(
    tmp_path,
) -> None:
    client = FakeRunnerClient(tmp_path)
    client.list_vulnerabilities = lambda _task_id: []
    runner = PhaseRunners(client)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "findings.md").write_text(
        "# 已确认结果\n\n疑似未授权访问管理接口，但尚未完成权限边界验证。",
        encoding="utf-8",
    )
    (run_dir / "initial_leads.md").write_text(
        "# 重要线索\n\n继续验证访问控制。",
        encoding="utf-8",
    )

    runner._reconcile_bootstrap_findings_contract(
        task_id="t_001",
        intent_id="i001",
        run_dir=run_dir,
    )

    findings = (run_dir / "findings.md").read_text(encoding="utf-8")
    assert "未形成结构化确认漏洞记录" in findings
    assert "疑似未授权访问管理接口" not in findings

    # A second reconciliation must be idempotent.
    runner._reconcile_bootstrap_findings_contract(
        task_id="t_001",
        intent_id="i001",
        run_dir=run_dir,
    )
    assert findings == (run_dir / "findings.md").read_text(encoding="utf-8")


def test_bootstrap_findings_are_rendered_from_structured_vulnerability_records(
    tmp_path,
) -> None:
    client = FakeRunnerClient(tmp_path)
    client.list_vulnerabilities = lambda _task_id: [
        {
            "id": "v001",
            "intent_id": "i001",
            "archived": False,
            "title": "越权读取",
            "severity": "高危",
            "type": "越权",
            "affected_target": "https://example.test/api/items/1",
            "status": "待确认",
            "description": "普通用户可以读取不属于自己的对象。",
            "prerequisites": "需要普通用户会话。",
            "impact": "造成越权读取。",
            "reproduction_steps": ["使用普通用户请求对象接口。"],
            "evidence": ["HTTP 200 且返回其他用户对象。"],
            "curl_command": "curl -H 'Authorization: Bearer secret-value' https://example.test",
        }
    ]
    runner = PhaseRunners(client)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "findings.md").write_text(
        "# 模型原始结果\n\n无关的未验证猜测",
        encoding="utf-8",
    )

    runner._reconcile_bootstrap_findings_contract(
        task_id="t_001",
        intent_id="i001",
        run_dir=run_dir,
    )

    findings = (run_dir / "findings.md").read_text(encoding="utf-8")
    assert "v001 · 越权读取" in findings
    assert "普通用户可以读取不属于自己的对象" in findings
    assert "secret-value" not in findings


def test_rpc_explore_retries_lightweight_closeout_without_reexecuting_intent(
    monkeypatch, tmp_path
) -> None:
    import axiom.dispatcher.v2.runners as runners_module

    class RpcDriver(ShellDriver):
        def supports_rpc(self):
            return True

        def prepare_session(self):
            return "rpc-session"

        def build_rpc(self, _worker, _session, phase=""):
            return ["/bin/true"]

    class FakeRpc:
        prompt_calls = 0
        follow_up_calls = 0

        def __init__(self, _command, env):
            self.env = env

        def start(self):
            pass

        def prompt(self, _message, **_kwargs):
            type(self).prompt_calls += 1
            write_explore_artifacts(SimpleNamespace(env=self.env))
            return RpcProcessResult(0, "plain text", "")

        def follow_up(self, _message, **_kwargs):
            type(self).follow_up_calls += 1
            return RpcProcessResult(0, "still invalid", "")

        def stop(self):
            pass

    monkeypatch.setattr(runners_module, "get_driver", lambda _name: RpcDriver())
    monkeypatch.setattr(runners_module, "ManagedRpcProcess", FakeRpc)
    client = explore_client(tmp_path)
    runner = PhaseRunners(client)
    closeout_responses = iter(
        [
            "not json",
            '{"accepted":true,"data":{"description":"收口结果见 findings.md，后续线索见 leads.md"}}',
        ]
    )
    closeout_calls = []

    def conclude(_worker, **kwargs):
        closeout_calls.append(kwargs["prompt"])
        return ProcessResult(0, "", ""), next(closeout_responses)

    monkeypatch.setattr(runner, "_conclude", conclude)
    runner.run(
        DispatchAction(kind="explore", task_id="t_001", intent_id="i002"),
        worker_snapshot(),
        RuntimeConfig(heartbeat_interval_seconds=1, explore_conclude_seconds=90),
    )

    assert FakeRpc.prompt_calls == 1
    assert FakeRpc.follow_up_calls == 2
    assert len(closeout_calls) == 0
    assert client.concluded[0][4] == "explore_artifact_summary"
    assert "findings.md" in client.concluded[0][3]


def test_missing_artifact_closeout_receives_full_remaining_budget(monkeypatch, tmp_path) -> None:
    """Cold artifact production must not be split into tiny restart loops."""
    import time

    client = explore_client(tmp_path)
    runner = PhaseRunners(client)
    worker = runner._worker(worker_snapshot()).model_copy(
        update={"env": {"AXIOM_RUN_DIR": str(tmp_path)}}
    )
    seen_timeouts = []

    def conclude(_worker, **kwargs):
        seen_timeouts.append(kwargs["timeout"])
        (tmp_path / "findings.md").write_text(
            "# 已确认结果\n本轮未确认新增漏洞。", encoding="utf-8"
        )
        (tmp_path / "leads.md").write_text(
            "# 后续线索\n继续验证访问控制。", encoding="utf-8"
        )
        return (
            ProcessResult(0, "", ""),
            '{"accepted":true,"data":{"description":"本轮验证 example.com 的认证与访问控制，匿名请求均被拒绝，未确认可利用漏洞；后续需使用不同角色账号复核对象级授权。详见 findings.md，未验证方向见 leads.md。"}}',
        )

    monkeypatch.setattr(runner, "_conclude", conclude)
    result = runner._run_closeout_attempts(
        task=client.task_value,
        worker=worker,
        task_id="t_001",
        intent=client.intent,
        phase="explore",
        context={"manifest_path": str(tmp_path / "manifest.json")},
        run_dir=tmp_path,
        session="rpc-session",
        evidence_path=tmp_path / "execute_evidence.md",
        compact=True,
        deadline=time.monotonic() + 90,
        lease=SimpleNamespace(),
        registered_artifacts={},
        usage_parts=[],
    )

    assert seen_timeouts == [pytest.approx(90, abs=1)]
    assert result[2] == []
    assert result[3] is not None


def test_rpc_provider_failure_does_not_retry_json_or_enter_conclude(monkeypatch, tmp_path) -> None:
    import axiom.dispatcher.v2.runners as runners_module

    class RpcDriver(ShellDriver):
        def supports_rpc(self):
            return True

        def prepare_session(self):
            return "rpc-session"

        def build_rpc(self, _worker, _session, phase=""):
            return ["/bin/true"]

    class FakeRpc:
        def __init__(self, _command, _env):
            pass

        def start(self):
            pass

        def prompt(self, _message, **_kwargs):
            return RpcProcessResult(1, "", "provider unavailable")

        def follow_up(self, *_args, **_kwargs):
            raise AssertionError("provider failure must not be treated as JSON repair")

        def stop(self):
            pass

    monkeypatch.setattr(runners_module, "get_driver", lambda _name: RpcDriver())
    monkeypatch.setattr(runners_module, "ManagedRpcProcess", FakeRpc)
    client = explore_client(tmp_path)
    runner = PhaseRunners(client)
    monkeypatch.setattr(
        runner,
        "_conclude",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("provider failure must not enter Conclude")
        ),
    )

    with pytest.raises(RunnerFailure, match="provider unavailable"):
        runner.run(
            DispatchAction(kind="explore", task_id="t_001", intent_id="i002"),
            worker_snapshot(),
            RuntimeConfig(heartbeat_interval_seconds=1),
        )
    assert len(client.released) == 1


def test_rpc_reason_commits_workspace_and_records_usage(monkeypatch, tmp_path) -> None:
    import axiom.dispatcher.v2.runners as runners_module

    class RpcDriver(ShellDriver):
        def supports_rpc(self):
            return True

        def prepare_session(self):
            return "rpc-session"

        def build_rpc(self, _worker, _session, phase=""):
            return ["/bin/true"]

        def extract_response_text(self, stdout, stderr):
            return "reason summary"

    class ReasonClient(FakeRunnerClient):
        def __init__(self, path):
            super().__init__(path)
            self.workspace_value = ReasonWorkspace(
                id="rw_001",
                task_id="t_001",
                status="open",
                context_revision=1,
                draft=WorkspaceDraft(),
                worker="worker-a",
                created_at="2026-07-17T00:00:00Z",
                updated_at="2026-07-17T00:00:00Z",
            )

        def claim_reason(self, *_args):
            return self.task_value

        def open_workspace(self, *_args, **_kwargs):
            return self.workspace_value

        def heartbeat_reason(self, *_args):
            return self.task_value

        def workspace(self, _workspace_id):
            return self.workspace_value

        def release_reason(self, *_args):
            self.reason_released.append(_args)

    client = ReasonClient(tmp_path)

    class FakeRpc:
        def __init__(self, _command, _env):
            pass

        def start(self):
            pass

        def prompt(self, _message, **_kwargs):
            client.workspace_value = client.workspace_value.model_copy(
                update={"status": "committed"}
            )
            usage = (
                '{"type":"agent_end","messages":['
                '{"role":"assistant","usage":'
                '{"input_tokens":20,"output_tokens":5,"total_tokens":25}}]}'
            )
            return RpcProcessResult(0, usage, "")

        def stop(self):
            pass

        def kill(self):
            pass

    monkeypatch.setattr(runners_module, "get_driver", lambda _name: RpcDriver())
    monkeypatch.setattr(runners_module, "ManagedRpcProcess", FakeRpc)
    runner = PhaseRunners(client)

    runner.run(
        DispatchAction(kind="reason", task_id="t_001"),
        worker_snapshot(),
        RuntimeConfig(heartbeat_interval_seconds=1),
    )

    assert client.reason_released
    committed = next(item for item in client.audit_events if item["event_type"] == "reason.committed")
    assert committed["payload"]["rpc"] is True
    assert committed["payload"]["token_usage"] == {
        "input_tokens": 20,
        "output_tokens": 5,
        "total_tokens": 25,
    }


def test_bootstrap_runner_records_token_usage_on_completion(monkeypatch, tmp_path) -> None:
    import axiom.dispatcher.v2.runners as runners_module

    monkeypatch.setattr(runners_module, "get_driver", lambda _name: UsageShellDriver())
    client = FakeRunnerClient(tmp_path)
    runner = PhaseRunners(client)

    runner.run(
        DispatchAction(kind="bootstrap", task_id="t_001", intent_id="i001"),
        worker_snapshot(),
        RuntimeConfig(heartbeat_interval_seconds=1),
    )

    completed = next(item for item in client.audit_events if item["event_type"] == "bootstrap.completed")
    assert completed["payload"]["token_usage"] == {
        "input_tokens": 123,
        "output_tokens": 45,
        "total_tokens": 168,
    }


def test_runner_registers_required_files_when_worker_omits_cli_registration(monkeypatch, tmp_path) -> None:
    import axiom.dispatcher.v2.runners as runners_module

    monkeypatch.setattr(runners_module, "get_driver", lambda _name: ArtifactShellDriver())
    client = FakeRunnerClient(tmp_path)
    runner = PhaseRunners(client)

    runner.run(
        DispatchAction(kind="bootstrap", task_id="t_001", intent_id="i001"),
        worker_snapshot(),
        RuntimeConfig(heartbeat_interval_seconds=1),
    )

    assert [item[1]["artifact_type"] for item in client.artifacts] == [
        "asset_inventory",
        "initial_leads",
        "findings",
    ]
    assert client.concluded


def test_execute_budget_exhaustion_recovers_session_and_forces_conclude(monkeypatch, tmp_path) -> None:
    import axiom.dispatcher.v2.runners as runners_module

    monkeypatch.setattr(runners_module, "get_driver", lambda _name: ShellDriver())
    client = FakeRunnerClient(tmp_path)
    runner = PhaseRunners(client)

    def execute(worker, **_kwargs):
        return ProcessResult(137, "", "", timed_out=True), "session-1", ""

    def conclude(worker, **_kwargs):
        run_dir = Path(worker.env["AXIOM_RUN_DIR"])
        (run_dir / "asset_inventory.md").write_text("inventory")
        (run_dir / "initial_leads.md").write_text("leads")
        (run_dir / "findings.md").write_text("findings")
        return ProcessResult(0, "summary", ""), "forced conclusion"

    monkeypatch.setattr(runner, "_execute", execute)
    monkeypatch.setattr(runner, "_conclude", conclude)
    runner.run(
        DispatchAction(kind="bootstrap", task_id="t_001", intent_id="i001"),
        worker_snapshot(),
        RuntimeConfig(heartbeat_interval_seconds=1),
    )

    assert client.released == []
    completed = next(item for item in client.audit_events if item["event_type"] == "bootstrap.completed")
    assert completed["payload"]["execute_budget_exhausted"] is True
    assert any(
        item["event_type"] == "bootstrap.execute_budget_exhausted"
        for item in client.audit_events
    )


def test_large_timeout_uses_compact_conclude(monkeypatch, tmp_path) -> None:
    import axiom.dispatcher.v2.runners as runners_module

    class CompactDriver(ShellDriver):
        def supports_compact_conclude(self):
            return True

        def session_size_bytes(self, _worker, _session):
            return 256 * 1024

        def export_compact_evidence(self, _worker, _session, destination):
            destination.write_text("evidence", encoding="utf-8")
            return destination

    monkeypatch.setattr(runners_module, "get_driver", lambda _name: CompactDriver())
    client = FakeRunnerClient(tmp_path)
    runner = PhaseRunners(client)

    monkeypatch.setattr(
        runner,
        "_execute",
        lambda *_args, **_kwargs: (ProcessResult(137, "", "", timed_out=True), "session-1", ""),
    )
    seen = {}

    def conclude(worker, **kwargs):
        seen.update(kwargs)
        run_dir = Path(worker.env["AXIOM_RUN_DIR"])
        for filename in ("asset_inventory.md", "initial_leads.md", "findings.md"):
            (run_dir / filename).write_text(filename, encoding="utf-8")
        return ProcessResult(0, "compact summary", ""), "compact conclusion"

    monkeypatch.setattr(runner, "_conclude", conclude)
    runner.run(
        DispatchAction(kind="bootstrap", task_id="t_001", intent_id="i001"),
        worker_snapshot(),
        RuntimeConfig(heartbeat_interval_seconds=1),
    )

    assert seen["compact"] is True
    assert "execute_evidence.md" in seen["prompt"]
    assert client.concluded


def test_conclude_timeout_after_complete_artifacts_is_success(monkeypatch, tmp_path) -> None:
    import axiom.dispatcher.v2.runners as runners_module

    monkeypatch.setattr(runners_module, "get_driver", lambda _name: ShellDriver())
    client = FakeRunnerClient(tmp_path)
    runner = PhaseRunners(client)

    monkeypatch.setattr(
        runner,
        "_execute",
        lambda *_args, **_kwargs: (ProcessResult(137, "", "", timed_out=True), "session-1", ""),
    )

    def conclude(worker, **_kwargs):
        run_dir = Path(worker.env["AXIOM_RUN_DIR"])
        for filename in ("asset_inventory.md", "initial_leads.md", "findings.md"):
            (run_dir / filename).write_text(filename, encoding="utf-8")
        return ProcessResult(137, "", "", timed_out=True), ""

    monkeypatch.setattr(runner, "_conclude", conclude)
    runner.run(
        DispatchAction(kind="bootstrap", task_id="t_001", intent_id="i001"),
        worker_snapshot(),
        RuntimeConfig(heartbeat_interval_seconds=1),
    )

    assert client.released == []
    assert client.concluded


def test_bootstrap_conclude_timeout_with_durable_scan_evidence_uses_fallback(
    monkeypatch, tmp_path
) -> None:
    import axiom.dispatcher.v2.runners as runners_module

    monkeypatch.setattr(runners_module, "get_driver", lambda _name: ShellDriver())
    client = FakeRunnerClient(tmp_path)
    runner = PhaseRunners(client)

    def execute(worker, **_kwargs):
        run_dir = Path(worker.env["AXIOM_RUN_DIR"])
        (run_dir / "katana-web.txt").write_text(
            "https://example.com/\nhttps://example.com/api/users\n",
            encoding="utf-8",
        )
        return ProcessResult(137, "", "", timed_out=True), "session-1", ""

    def conclude(_worker, **_kwargs):
        return ProcessResult(137, "", "", timed_out=True), ""

    monkeypatch.setattr(runner, "_execute", execute)
    monkeypatch.setattr(runner, "_conclude", conclude)

    runner.run(
        DispatchAction(kind="bootstrap", task_id="t_001", intent_id="i001"),
        worker_snapshot(),
        RuntimeConfig(heartbeat_interval_seconds=1, bootstrap_conclude_seconds=1),
    )

    assert client.released == []
    assert client.concluded
    assert {item[1]["artifact_type"] for item in client.artifacts} == {
        "asset_inventory",
        "initial_leads",
        "findings",
    }
    findings = next(
        item[1]["path"] for item in client.artifacts if item[1]["artifact_type"] == "findings"
    )
    assert "未形成满足漏洞标准" in Path(findings).read_text(encoding="utf-8")
    assert any(
        item["event_type"] == "bootstrap.artifact_fallback" for item in client.audit_events
    )


def test_bootstrap_conclude_session_failure_with_durable_evidence_uses_fallback(
    monkeypatch, tmp_path
) -> None:
    import axiom.dispatcher.v2.runners as runners_module

    monkeypatch.setattr(runners_module, "get_driver", lambda _name: ShellDriver())
    client = FakeRunnerClient(tmp_path)
    runner = PhaseRunners(client)

    def execute(worker, **_kwargs):
        run_dir = Path(worker.env["AXIOM_RUN_DIR"])
        (run_dir / "httpx-targets.txt").write_text(
            "https://example.com [200] [Example]\n",
            encoding="utf-8",
        )
        return ProcessResult(137, "", "", timed_out=True), "missing-session", ""

    monkeypatch.setattr(runner, "_execute", execute)
    monkeypatch.setattr(
        runner,
        "_conclude",
        lambda *_args, **_kwargs: (
            ProcessResult(1, "", "No session found matching 'missing-session'"),
            "",
        ),
    )

    runner.run(
        DispatchAction(kind="bootstrap", task_id="t_001", intent_id="i001"),
        worker_snapshot(),
        RuntimeConfig(heartbeat_interval_seconds=1),
    )

    assert client.released == []
    assert client.concluded
    event_types = [item["event_type"] for item in client.audit_events]
    assert "bootstrap.conclude_process_fallback" in event_types
    assert "bootstrap.artifact_fallback" in event_types


def test_execute_budget_exhaustion_without_session_fails(monkeypatch, tmp_path) -> None:
    import axiom.dispatcher.v2.runners as runners_module

    monkeypatch.setattr(runners_module, "get_driver", lambda _name: ShellDriver())
    client = FakeRunnerClient(tmp_path)
    runner = PhaseRunners(client)
    monkeypatch.setattr(
        runner,
        "_execute",
        lambda *_args, **_kwargs: (ProcessResult(137, "", "", timed_out=True), None, ""),
    )

    with pytest.raises(RunnerFailure, match="session could not be recovered"):
        runner.run(
            DispatchAction(kind="bootstrap", task_id="t_001", intent_id="i001"),
            worker_snapshot(),
            RuntimeConfig(heartbeat_interval_seconds=1),
        )
    assert client.released
    assert client.concluded == []


def test_missing_required_artifact_never_auto_finalizes_success(monkeypatch, tmp_path) -> None:
    import axiom.dispatcher.v2.runners as runners_module

    class MissingArtifactDriver(ShellDriver):
        def build_execute(self, worker, prompt, session, phase=""):
            return DriverResult(argv=["/bin/sh", "-c", "printf summary"], session=None)

    monkeypatch.setattr(runners_module, "get_driver", lambda _name: MissingArtifactDriver())
    client = FakeRunnerClient(tmp_path)
    runner = PhaseRunners(client)

    with pytest.raises(RunnerFailure, match="required artifacts missing"):
        runner.run(
            DispatchAction(kind="bootstrap", task_id="t_001", intent_id="i001"),
            worker_snapshot(),
            RuntimeConfig(heartbeat_interval_seconds=1),
        )
    assert client.concluded == []
    assert client.artifacts == []


def test_bootstrap_bundle_materializes_three_artifacts_atomically(tmp_path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "bootstrap_artifacts.json").write_text(
        json.dumps(
            {
                "asset_inventory": "# Inventory\nweb 200",
                "initial_leads": "# Leads\n/api/v1/admin/products",
                "findings": "# Findings\nNo vulnerability confirmed",
            }
        ),
        encoding="utf-8",
    )

    PhaseRunners._materialize_artifact_bundle(run_dir=run_dir, phase="bootstrap")

    assert (run_dir / "asset_inventory.md").read_text() == "# Inventory\nweb 200\n"
    assert (run_dir / "initial_leads.md").read_text() == "# Leads\n/api/v1/admin/products\n"
    assert (run_dir / "findings.md").read_text() == "# Findings\nNo vulnerability confirmed\n"


def test_phase_env_injects_canonical_tool_paths(monkeypatch, tmp_path) -> None:
    import axiom.dispatcher.v2.runners as runners_module

    resolved = {
        "httpx": "/opt/axiom-tools/httpx",
        "nmap": "/opt/axiom-tools/nmap",
        "subfinder": "/opt/axiom-tools/subfinder",
        "katana": "/opt/axiom-tools/katana",
        "nuclei": "/opt/axiom-tools/nuclei",
    }
    monkeypatch.setattr(
        runners_module.shutil,
        "which",
        lambda name, path=None: resolved.get(name),
    )
    client = FakeRunnerClient(tmp_path)
    runner = PhaseRunners(client)
    worker = runner._worker(worker_snapshot())
    context = {"manifest_path": str(tmp_path / "manifest.json")}
    env = runner._phase_env(
        worker,
        task=client.task_value,
        phase="bootstrap",
        intent_id="i001",
        context=context,
        run_dir=tmp_path / "run",
    )

    assert env["AXIOM_TOOL_HTTPX"] == resolved["httpx"]
    assert env["AXIOM_TOOL_NMAP"] == resolved["nmap"]
    assert json.loads(env["AXIOM_TOOL_PATHS_JSON"])["KATANA"] == resolved["katana"]
    assert "/opt/axiom-tools" in env["PATH"].split(":")


def test_rpc_bootstrap_conclude_uses_one_bundle_and_real_conclude_budget(monkeypatch, tmp_path) -> None:
    import axiom.dispatcher.v2.runners as runners_module

    seen_sessions = {}

    class BundleRpcDriver(ShellDriver):
        def supports_rpc(self):
            return True

        def prepare_session(self):
            return "rpc-session"

        def build_rpc(self, _worker, _session, phase=""):
            return ["/bin/true"]

        def recover_session(self, _worker, session, _stdout, _stderr):
            seen_sessions["prepared"] = session
            return "actual-rpc-session"

        def supports_compact_conclude(self):
            return True

        def build_compact_conclude(self, _worker, _prompt, phase=""):
            bundle = json.dumps(
                {
                    "asset_inventory": "# Inventory\nweb 200",
                    "initial_leads": "# Leads\n/api/v1/admin/products",
                    "findings": "# Findings\nNo vulnerability confirmed",
                }
            )
            command = f"printf '%s' '{bundle}' > \"$AXIOM_RUN_DIR/bootstrap_artifacts.json\""
            return ["/bin/sh", "-c", command]

        def export_compact_evidence(self, _worker, _session, destination):
            seen_sessions["evidence"] = _session
            destination.write_text("# evidence", encoding="utf-8")
            return destination

    class FakeRpc:
        def __init__(self, _command, env):
            self.env = env

        def start(self):
            pass

        def prompt(self, _message, **_kwargs):
            return RpcProcessResult(0, "execute summary", "")

        def stop(self):
            pass

    monkeypatch.setattr(runners_module, "get_driver", lambda _name: BundleRpcDriver())
    monkeypatch.setattr(runners_module, "ManagedRpcProcess", FakeRpc)
    client = FakeRunnerClient(tmp_path)
    runner = PhaseRunners(client)

    runner.run(
        DispatchAction(kind="bootstrap", task_id="t_001", intent_id="i001"),
        worker_snapshot(),
        RuntimeConfig(heartbeat_interval_seconds=1, bootstrap_conclude_seconds=2),
    )

    assert [item[1]["artifact_type"] for item in client.artifacts] == [
        "asset_inventory",
        "initial_leads",
        "findings",
    ]
    event_types = [item["event_type"] for item in client.audit_events]
    assert "bootstrap.conclude_started" in event_types
    assert "bootstrap.conclude_completed" in event_types
    assert "bootstrap.session_recovered" in event_types
    assert seen_sessions == {
        "prepared": "rpc-session",
        "evidence": "actual-rpc-session",
    }
