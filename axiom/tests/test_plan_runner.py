from __future__ import annotations

import json
from pathlib import Path

from axiom.dispatcher.plan import PlanRunner
from axiom.dispatcher.pi import PiPlanExecutor
from axiom.modules.execution.schemas import PlanExecutionInput
from axiom.modules.plan.schemas import (
    ClaimedPlanRun,
    PlanCommitResult,
    PlanWorkspace,
    PlanWorkspaceStatus,
)
from axiom.modules.workers.schemas import Worker


def _work() -> ClaimedPlanRun:
    return ClaimedPlanRun(
        task_id="t_001",
        plan_run_id="pr000001",
        worker_name="plan-worker",
        lease_id="lease-plan",
        claimed_at="2026-08-21T00:00:00Z",
    )


def _execution_input(tmp_path: Path) -> PlanExecutionInput:
    run_dir = tmp_path / "plan-run"
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = run_dir / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    return PlanExecutionInput(
        task_id="t_001",
        project_id="p_001",
        plan_run_id="pr000001",
        lease_id="lease-plan",
        worker_name="plan-worker",
        origin="https://target.example",
        goal="完成全局攻击面复盘",
        scope={"enabled": True, "targets": ["target.example"]},
        run_dir=str(run_dir),
        context_revision=1,
        context_manifest_path=str(manifest),
        execute_timeout_seconds=10,
        conclude_timeout_seconds=10,
    )


def _workspace() -> PlanWorkspace:
    return PlanWorkspace(
        id="pw000001",
        task_id="t_001",
        plan_run_id="pr000001",
        lease_id="lease-plan",
        status="open",
        context_revision=1,
        draft={
            "decision": {
                "decision": "exhausted",
                "reviewed_dimensions": ["coverage"],
                "exhaustion_reason": "已复核全部材料",
                "evidence_refs": ["fact:f001"],
                "confidence": "high",
            }
        },
        worker="plan-worker",
        created_at="2026-08-21T00:00:00Z",
        updated_at="2026-08-21T00:00:00Z",
    )


def _worker(script: Path, environment: dict[str, str]) -> Worker:
    return Worker(
        name="plan-worker",
        worker_type="pi",
        capabilities=["plan"],
        max_running=1,
        priority=0,
        config={
            "binary": str(script),
            "model": "test-model",
            "environment": environment,
        },
        disabled=False,
        created_at="2026-08-21T00:00:00Z",
        updated_at="2026-08-21T00:00:00Z",
    )


def test_plan_runner_uses_the_independent_plan_workspace_protocol(tmp_path: Path) -> None:
    events: list[str] = []
    execution_input = _execution_input(tmp_path)
    workspace = _workspace()

    class ControlPlane:
        def start(self, _work):
            events.append("start")

        def heartbeat(self, _work):
            events.append("heartbeat")

        def input(self, _work):
            events.append("input")
            return execution_input

        def open_plan_workspace(self, _work, *, context_revision):
            assert context_revision == 1
            events.append("open_plan_workspace")
            return workspace

        def activity(self, _work, command):
            assert command.activity_kind == "analyzing"
            events.append("activity")

        def plan_workspace_status(self, _work, *, workspace_id):
            assert workspace_id == workspace.id
            events.append("plan_workspace_status")
            return PlanWorkspaceStatus(
                workspace_id=workspace.id,
                status="open",
                decision=workspace.draft.decision,
                ready=True,
            )

        def commit_plan_workspace(self, _work, *, workspace_id):
            assert workspace_id == workspace.id
            events.append("commit_plan_workspace")
            return PlanCommitResult(workspace=workspace)

        def release(self, *_args, **_kwargs):
            raise AssertionError("successful Plan must not be released")

    class Executor:
        def execute(self, value, *, workspace, lease_active):
            assert isinstance(value, PlanExecutionInput)
            assert workspace.plan_run_id == value.plan_run_id
            assert lease_active()
            events.append("execute_plan")
            return "FINISH"

        def conclude(self, *_args, **_kwargs):
            raise AssertionError("valid Plan must not enter conclude")

    result = PlanRunner(
        ControlPlane(),  # type: ignore[arg-type]
        heartbeat_interval_seconds=60,
    ).run(_work(), Executor())

    assert result.workspace.id == workspace.id
    assert events == [
        "start",
        "heartbeat",
        "input",
        "open_plan_workspace",
        "activity",
        "execute_plan",
        "plan_workspace_status",
        "commit_plan_workspace",
    ]


def test_pi_plan_executor_sets_plan_identity_and_uses_only_plan_prompt(
    tmp_path: Path,
) -> None:
    environment_log = tmp_path / "plan-env.json"
    prompt_log = tmp_path / "plan-prompt.txt"
    script = tmp_path / "fake-pi"
    script.write_text(
        "#!/bin/sh\n"
        "printf '{\"phase\":\"%s\",\"run\":\"%s\",\"workspace\":\"%s\"}' "
        '"$AXIOM_PHASE" "$AXIOM_PLAN_RUN_ID" "$AXIOM_PLAN_WORKSPACE_ID" > "$ENV_LOG"\n'
        "previous=''\n"
        'for value in "$@"; do\n'
        '  if [ "$previous" = "-p" ]; then printf "%s" "$value" > "$PROMPT_LOG"; fi\n'
        '  previous="$value"\n'
        "done\n"
        "printf 'FINISH'\n",
        encoding="utf-8",
    )
    script.chmod(0o755)

    output = PiPlanExecutor(
        _worker(
            script,
            {
                "ENV_LOG": str(environment_log),
                "PROMPT_LOG": str(prompt_log),
            },
        )
    ).execute(
        _execution_input(tmp_path),
        workspace=_workspace(),
        lease_active=lambda: True,
    )

    assert output == "FINISH"
    assert json.loads(environment_log.read_text(encoding="utf-8")) == {
        "phase": "plan",
        "run": "pr000001",
        "workspace": "pw000001",
    }
    prompt = prompt_log.read_text(encoding="utf-8")
    assert "AXIOM 全景复盘阶段（Plan）" in prompt
    assert "# AXIOM 推理规划阶段（Reason）" not in prompt
    assert "reason_default.md" not in prompt
    assert "reason_conclude.md" not in prompt


def test_pi_plan_conclude_uses_only_plan_conclude_prompt(tmp_path: Path) -> None:
    prompt_log = tmp_path / "plan-conclude-prompt.txt"
    script = tmp_path / "fake-pi"
    script.write_text(
        "#!/bin/sh\n"
        "previous=''\n"
        'for value in "$@"; do\n'
        '  if [ "$previous" = "-p" ]; then printf "%s" "$value" > "$PROMPT_LOG"; fi\n'
        '  previous="$value"\n'
        "done\n"
        "printf 'FINISH'\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    executor = PiPlanExecutor(
        _worker(script, {"PROMPT_LOG": str(prompt_log)})
    )
    executor._session_id = "plan-session"

    output = executor.conclude(
        _execution_input(tmp_path),
        workspace=_workspace(),
        reason="missing decision",
        lease_active=lambda: True,
    )

    assert output == "FINISH"
    prompt = prompt_log.read_text(encoding="utf-8")
    assert "# Plan 收口协议" in prompt
    assert "reason_conclude.md" not in prompt
    assert "Reason Workspace" not in prompt
