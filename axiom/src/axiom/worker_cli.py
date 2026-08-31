from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import click
import httpx


TODO_CATEGORIES = (
    "recon",
    "authentication",
    "authorization",
    "injection",
    "business_logic",
    "file",
    "network",
    "configuration",
    "client_side",
    "other",
)
TODO_STATUSES = ("open", "partial", "blocked", "completed")
SOURCE_TYPES = {"fact", "hint", "artifact", "vulnerability", "knowledge", "origin"}


def register_worker_commands(main: click.Group) -> None:
    @main.group("credential")
    def credential_group() -> None:
        """Read enabled Project credentials through the current active lease."""

    @credential_group.command("list")
    def credential_list() -> None:
        task_id = _environment("AXIOM_TASK_ID")
        _echo(
            _request(
                "POST",
                f"/dispatch/tasks/{task_id}/credentials/list",
                _credential_access_payload(),
            )
        )

    @credential_group.command("get")
    @click.argument("credential_id")
    @click.option(
        "--output",
        type=click.Path(path_type=Path, dir_okay=False),
        required=True,
    )
    def credential_get(credential_id: str, output: Path) -> None:
        task_id = _environment("AXIOM_TASK_ID")
        credential = _request(
            "POST",
            f"/dispatch/tasks/{task_id}/credentials/{credential_id}/get",
            _credential_access_payload(),
        )
        if not isinstance(credential, dict):
            raise click.ClickException("AXIOM API returned an invalid credential")
        destination = _write_private_text(output, _credential_markdown(credential))
        _echo(
            {
                "id": str(credential.get("id") or credential_id),
                "output": str(destination),
            }
        )

    @credential_group.command("refresh")
    @click.argument("refresh_id")
    @click.option(
        "--output",
        type=click.Path(path_type=Path, dir_okay=False),
        required=True,
    )
    def credential_refresh(refresh_id: str, output: Path) -> None:
        task_id = _environment("AXIOM_TASK_ID")
        refresh = _request(
            "POST",
            f"/dispatch/tasks/{task_id}/credential-refreshes/{refresh_id}/get",
            _credential_access_payload(),
        )
        if not isinstance(refresh, dict) or not isinstance(refresh.get("content"), str):
            raise click.ClickException("AXIOM API returned invalid refresh instructions")
        destination = _write_private_text(output, refresh["content"])
        _echo(
            {
                "id": str(refresh.get("id") or refresh_id),
                "output": str(destination),
            }
        )

    @main.group("reason")
    def reason_group() -> None:
        """Incrementally edit the active Reason Workspace over HTTP."""

    @reason_group.command("status")
    def reason_status() -> None:
        _echo(_workspace_request("POST", "status", {}))

    @reason_group.group("todo")
    def reason_todo_group() -> None:
        """Add or update one shared Todo."""

    @reason_todo_group.command("add")
    @click.option("--ref", "todo_ref", required=True)
    @click.option("--title")
    @click.option("--title-file", type=click.Path(path_type=Path, dir_okay=False))
    @click.option("--category", type=click.Choice(TODO_CATEGORIES), required=True)
    @click.option("--theme", default="")
    @click.option("--priority", type=click.IntRange(0, 100), default=50, show_default=True)
    @click.option("--status", type=click.Choice(TODO_STATUSES), default="open")
    @click.option("--status-reason", default="")
    @click.option("--status-reason-file", type=click.Path(path_type=Path, dir_okay=False))
    @click.option("--source", multiple=True, required=True, help="Repeat kind:id sources.")
    def reason_todo_add(
        todo_ref: str,
        title: str | None,
        title_file: Path | None,
        category: str,
        theme: str,
        priority: int,
        status: str,
        status_reason: str,
        status_reason_file: Path | None,
        source: tuple[str, ...],
    ) -> None:
        payload = {
            "todo": {
                "ref": todo_ref,
                "title": _text_value(title, title_file, "title", required=True),
                "category": category,
                "theme": theme,
                "priority": priority,
                "status": status,
                "status_reason": _text_value(
                    status_reason,
                    status_reason_file,
                    "status reason",
                ),
                "sources": [_source(value) for value in source],
            }
        }
        _echo_workspace(_workspace_request("POST", "todos", payload))

    @reason_todo_group.command("update")
    @click.option("--todo-id", required=True, help="Existing Todo ID or this Workspace's draft ref.")
    @click.option("--title")
    @click.option("--title-file", type=click.Path(path_type=Path, dir_okay=False))
    @click.option("--category", type=click.Choice(TODO_CATEGORIES))
    @click.option("--theme")
    @click.option("--priority", type=click.IntRange(0, 100))
    @click.option("--status", type=click.Choice(TODO_STATUSES))
    @click.option("--status-reason")
    @click.option("--status-reason-file", type=click.Path(path_type=Path, dir_okay=False))
    @click.option("--source-add", multiple=True, help="Repeat kind:id sources.")
    def reason_todo_update(
        todo_id: str,
        title: str | None,
        title_file: Path | None,
        category: str | None,
        theme: str | None,
        priority: int | None,
        status: str | None,
        status_reason: str | None,
        status_reason_file: Path | None,
        source_add: tuple[str, ...],
    ) -> None:
        update: dict[str, Any] = {"todo_id": todo_id}
        _set_optional_text(update, "title", title, title_file)
        if category is not None:
            update["category"] = category
        if theme is not None:
            update["theme"] = theme
        if priority is not None:
            update["priority"] = priority
        if status is not None:
            update["status"] = status
        _set_optional_text(
            update,
            "status_reason",
            status_reason,
            status_reason_file,
        )
        if source_add:
            update["source_adds"] = [_source(value) for value in source_add]
        if len(update) == 1:
            raise click.UsageError("todo update requires at least one changed field")
        _echo_workspace(_workspace_request("POST", "todo-updates", {"update": update}))

    @reason_todo_group.command("delete")
    @click.option("--todo-id", required=True, help="Existing Todo ID or this Workspace's draft ref.")
    @click.option("--reason", required=True, help="Reason for deleting or archiving this Todo (e.g. duplicate).")
    @click.option("--reason-file", type=click.Path(path_type=Path, dir_okay=False))
    def reason_todo_delete(
        todo_id: str,
        reason: str | None,
        reason_file: Path | None,
    ) -> None:
        deletion = {
            "todo_id": todo_id,
            "reason": _text_value(reason, reason_file, "reason", required=True),
        }
        _echo_workspace(_workspace_request("POST", "todo-deletions", {"deletion": deletion}))

    @reason_group.group("intent")
    def reason_intent_group() -> None:
        """Propose one pending Explore Intent."""

    @reason_intent_group.command("propose")
    @click.option("--todo-ref")
    @click.option("--todo-id")
    @click.option("--from-fact", multiple=True, required=True)
    @click.option("--description")
    @click.option("--title", help="Alias for --description to support resilient model calls.")
    @click.option("--description-file", type=click.Path(path_type=Path, dir_okay=False))
    @click.option(
        "--target",
        multiple=True,
        help="Optional without Task Scope; repeat for scoped or multi-target work.",
    )
    @click.option("--priority", type=click.IntRange(0, 100), default=50, show_default=True)
    @click.option("--execute-seconds", type=click.IntRange(1), required=True)
    @click.option("--conclude-seconds", type=click.IntRange(1), required=True)
    def reason_intent_propose(
        todo_ref: str | None,
        todo_id: str | None,
        from_fact: tuple[str, ...],
        description: str | None,
        title: str | None,
        description_file: Path | None,
        target: tuple[str, ...],
        priority: int,
        execute_seconds: int,
        conclude_seconds: int,
    ) -> None:
        if bool(todo_ref) == bool(todo_id):
            raise click.UsageError("exactly one of --todo-ref or --todo-id is required")
        raw_description = description if description is not None else title
        proposal = {
            "todo_ref": todo_ref,
            "todo_id": todo_id,
            "from_facts": list(from_fact),
            "description": _text_value(
                raw_description,
                description_file,
                "description",
                required=True,
            ),
            "targets": list(target),
            "priority": priority,
            "execute_seconds": execute_seconds,
            "conclude_seconds": conclude_seconds,
        }
        _echo_workspace(_workspace_request("POST", "intents", {"intent": proposal}))

    @reason_intent_group.command("assess")
    @click.option("--intent-id", required=True, help="Terminal Explore Intent ID to assess.")
    @click.option(
        "--status",
        "--assessment-status",
        type=click.Choice(("partial", "blocked", "completed")),
        required=True,
        help="One-time evaluation of the finished Intent.",
    )
    @click.option("--reason", "--assessment-reason", "assessment_reason")
    @click.option(
        "--reason-file",
        "--assessment-reason-file",
        type=click.Path(path_type=Path, dir_okay=False),
    )
    def reason_intent_assess(
        intent_id: str,
        status: str,
        assessment_reason: str | None,
        reason_file: Path | None,
    ) -> None:
        assessment = {
            "intent_id": intent_id,
            "status": status,
            "reason": _text_value(
                assessment_reason,
                reason_file,
                "reason",
                required=True,
            ),
        }
        _echo_workspace(
            _workspace_request(
                "POST",
                "intent-assessments",
                {"assessment": assessment},
            )
        )

    @reason_group.group("goal")
    def reason_goal_group() -> None:
        """Assess whether the Task Goal has been achieved."""

    @reason_goal_group.command("assess")
    @click.option("--achieved/--not-achieved", default=None)
    @click.option("--reason", "assessment_reason")
    @click.option("--reason-file", type=click.Path(path_type=Path, dir_okay=False))
    @click.option(
        "--evidence",
        multiple=True,
        help="兼容旧调用的可选引用；Goal 完成不要求 Fact evidence。",
    )
    def reason_goal_assess(
        achieved: bool | None,
        assessment_reason: str | None,
        reason_file: Path | None,
        evidence: tuple[str, ...],
    ) -> None:
        if achieved is None:
            raise click.UsageError("choose --achieved or --not-achieved")
        assessment = {
            "achieved": achieved,
            "reason": _text_value(
                assessment_reason,
                reason_file,
                "reason",
                required=True,
            ),
            "evidence_refs": list(evidence),
        }
        _echo_workspace(
            _workspace_request("POST", "goal-assessment", {"assessment": assessment})
        )

    @main.group("plan")
    def plan_group() -> None:
        """Edit the active Plan Workspace over the independent Plan protocol."""

    @plan_group.command("status")
    def plan_status() -> None:
        _echo(_plan_workspace_request("status", {}))

    @plan_group.group("todo")
    def plan_todo_group() -> None:
        """Add, update, or archive a Todo during panorama planning."""

    @plan_todo_group.command("add")
    @click.option("--ref", "todo_ref", required=True)
    @click.option("--title")
    @click.option("--title-file", type=click.Path(path_type=Path, dir_okay=False))
    @click.option("--category", type=click.Choice(TODO_CATEGORIES), required=True)
    @click.option("--theme", default="")
    @click.option("--priority", type=click.IntRange(0, 100), default=50, show_default=True)
    @click.option("--status", type=click.Choice(TODO_STATUSES), default="open")
    @click.option("--status-reason", default="")
    @click.option("--status-reason-file", type=click.Path(path_type=Path, dir_okay=False))
    @click.option("--source", multiple=True, required=True, help="Repeat kind:id sources.")
    def plan_todo_add(
        todo_ref: str,
        title: str | None,
        title_file: Path | None,
        category: str,
        theme: str,
        priority: int,
        status: str,
        status_reason: str,
        status_reason_file: Path | None,
        source: tuple[str, ...],
    ) -> None:
        _echo_plan_workspace(
            _plan_workspace_request(
                "todos",
                {
                    "todo": {
                        "ref": todo_ref,
                        "title": _text_value(title, title_file, "title", required=True),
                        "category": category,
                        "theme": theme,
                        "priority": priority,
                        "status": status,
                        "status_reason": _text_value(
                            status_reason,
                            status_reason_file,
                            "status reason",
                        ),
                        "sources": [_source(value) for value in source],
                    }
                },
            )
        )

    @plan_todo_group.command("update")
    @click.option("--todo-id", required=True)
    @click.option("--title")
    @click.option("--title-file", type=click.Path(path_type=Path, dir_okay=False))
    @click.option("--category", type=click.Choice(TODO_CATEGORIES))
    @click.option("--theme")
    @click.option("--priority", type=click.IntRange(0, 100))
    @click.option("--status", type=click.Choice(TODO_STATUSES))
    @click.option("--status-reason")
    @click.option("--status-reason-file", type=click.Path(path_type=Path, dir_okay=False))
    @click.option("--source-add", multiple=True, help="Repeat kind:id sources.")
    def plan_todo_update(
        todo_id: str,
        title: str | None,
        title_file: Path | None,
        category: str | None,
        theme: str | None,
        priority: int | None,
        status: str | None,
        status_reason: str | None,
        status_reason_file: Path | None,
        source_add: tuple[str, ...],
    ) -> None:
        update: dict[str, Any] = {"todo_id": todo_id}
        _set_optional_text(update, "title", title, title_file)
        if category is not None:
            update["category"] = category
        if theme is not None:
            update["theme"] = theme
        if priority is not None:
            update["priority"] = priority
        if status is not None:
            update["status"] = status
        _set_optional_text(update, "status_reason", status_reason, status_reason_file)
        if source_add:
            update["source_adds"] = [_source(value) for value in source_add]
        if len(update) == 1:
            raise click.UsageError("todo update requires at least one changed field")
        _echo_plan_workspace(
            _plan_workspace_request("todo-updates", {"update": update})
        )

    @plan_todo_group.command("delete")
    @click.option("--todo-id", required=True)
    @click.option("--reason")
    @click.option("--reason-file", type=click.Path(path_type=Path, dir_okay=False))
    def plan_todo_delete(
        todo_id: str,
        reason: str | None,
        reason_file: Path | None,
    ) -> None:
        _echo_plan_workspace(
            _plan_workspace_request(
                "todo-deletions",
                {
                    "deletion": {
                        "todo_id": todo_id,
                        "reason": _text_value(
                            reason,
                            reason_file,
                            "reason",
                            required=True,
                        ),
                    }
                },
            )
        )

    @plan_group.group("intent")
    def plan_intent_group() -> None:
        """Propose a new Explore Intent from the panorama review."""

    @plan_intent_group.command("propose")
    @click.option("--todo-ref")
    @click.option("--todo-id")
    @click.option("--from-fact", multiple=True)
    @click.option("--source-ref", multiple=True)
    @click.option("--description")
    @click.option("--description-file", type=click.Path(path_type=Path, dir_okay=False))
    @click.option("--why-now", required=True)
    @click.option("--endpoint", default="")
    @click.option("--test-vector", required=True)
    @click.option("--expected-signal", required=True)
    @click.option("--stop-condition", required=True)
    @click.option("--dedup-fingerprint", default="")
    @click.option("--target", multiple=True)
    @click.option("--priority", type=click.IntRange(0, 100), default=50, show_default=True)
    @click.option("--execute-seconds", type=click.IntRange(1), required=True)
    @click.option("--conclude-seconds", type=click.IntRange(1), required=True)
    def plan_intent_propose(
        todo_ref: str | None,
        todo_id: str | None,
        from_fact: tuple[str, ...],
        source_ref: tuple[str, ...],
        description: str | None,
        description_file: Path | None,
        why_now: str,
        endpoint: str,
        test_vector: str,
        expected_signal: str,
        stop_condition: str,
        dedup_fingerprint: str,
        target: tuple[str, ...],
        priority: int,
        execute_seconds: int,
        conclude_seconds: int,
    ) -> None:
        if bool(todo_ref) == bool(todo_id):
            raise click.UsageError("exactly one of --todo-ref or --todo-id is required")
        if not from_fact and not source_ref:
            raise click.UsageError("provide at least one --from-fact or --source-ref")
        _echo_plan_workspace(
            _plan_workspace_request(
                "intents",
                {
                    "intent": {
                        "todo_ref": todo_ref,
                        "todo_id": todo_id,
                        "from_facts": list(from_fact),
                        "source_refs": list(source_ref),
                        "description": _text_value(
                            description,
                            description_file,
                            "description",
                            required=True,
                        ),
                        "why_now": why_now,
                        "endpoint": endpoint,
                        "test_vector": test_vector,
                        "expected_signal": expected_signal,
                        "stop_condition": stop_condition,
                        "dedup_fingerprint": dedup_fingerprint,
                        "targets": list(target),
                        "priority": priority,
                        "execute_seconds": execute_seconds,
                        "conclude_seconds": conclude_seconds,
                    }
                },
            )
        )

    @plan_group.group("decision")
    def plan_decision_group() -> None:
        """Submit the Plan handoff decision."""

    @plan_decision_group.command("submit")
    @click.argument(
        "decision",
        type=click.Choice(("continue", "exhausted", "needs_reason", "dispatch_explore")),
    )
    @click.option("--reviewed-dimension", multiple=True)
    @click.option("--exhaustion-reason", default="")
    @click.option("--evidence", multiple=True)
    @click.option(
        "--confidence",
        type=click.Choice(("high", "medium", "low", "unspecified")),
        default="unspecified",
    )
    def plan_decision_submit(
        decision: str,
        reviewed_dimension: tuple[str, ...],
        exhaustion_reason: str,
        evidence: tuple[str, ...],
        confidence: str,
    ) -> None:
        if decision == "exhausted" and not exhaustion_reason.strip():
            raise click.UsageError("exhausted requires --exhaustion-reason")
        _echo_plan_workspace(
            _plan_workspace_request(
                "decision",
                {
                    "decision": {
                        "decision": decision,
                        "reviewed_dimensions": list(reviewed_dimension),
                        "exhaustion_reason": exhaustion_reason,
                        "evidence_refs": list(evidence),
                        "confidence": confidence,
                    }
                },
            )
        )

    def _submit_plan_exhaustion(
        reason: str,
        reviewed_dimension: tuple[str, ...],
        evidence: tuple[str, ...],
        confidence: str,
    ) -> None:
        _echo_plan_workspace(
            _plan_workspace_request(
                "decision",
                {
                    "decision": {
                        "decision": "exhausted",
                        "reviewed_dimensions": list(reviewed_dimension),
                        "exhaustion_reason": reason,
                        "evidence_refs": list(evidence),
                        "confidence": confidence,
                    }
                },
            )
        )

    @plan_group.group("exhaustion", invoke_without_command=True)
    @click.option("--reason")
    @click.option("--reviewed-dimension", multiple=True)
    @click.option("--evidence", multiple=True)
    @click.option(
        "--confidence",
        type=click.Choice(("high", "medium", "low")),
    )
    @click.pass_context
    def plan_exhaustion_group(
        context: click.Context,
        reason: str | None,
        reviewed_dimension: tuple[str, ...],
        evidence: tuple[str, ...],
        confidence: str | None,
    ) -> None:
        """Submit the Plan exhaustion proof.

        The nested ``submit`` command is the canonical spelling.  Direct
        ``axiom plan exhaustion --reason ...`` remains accepted for workers
        using the pre-independent-Plan CLI.
        """
        if context.invoked_subcommand is not None:
            return
        if not reason or not reason.strip():
            raise click.UsageError(
                "missing command; use 'exhaustion submit' or provide --reason"
            )
        if not reviewed_dimension:
            raise click.UsageError("--reviewed-dimension is required")
        if confidence is None:
            raise click.UsageError("--confidence is required")
        _submit_plan_exhaustion(reason, reviewed_dimension, evidence, confidence)

    @plan_exhaustion_group.command("submit")
    @click.option("--reason", required=True)
    @click.option("--reviewed-dimension", multiple=True, required=True)
    @click.option("--evidence", multiple=True)
    @click.option(
        "--confidence",
        type=click.Choice(("high", "medium", "low")),
        required=True,
    )
    def plan_exhaustion_submit(
        reason: str,
        reviewed_dimension: tuple[str, ...],
        evidence: tuple[str, ...],
        confidence: str,
    ) -> None:
        _submit_plan_exhaustion(reason, reviewed_dimension, evidence, confidence)

    @main.group("explore")
    def explore_group() -> None:
        """Explore-stage code-maintained operations."""

    @explore_group.group("vulnerability")
    def vulnerability_group() -> None:
        """Report one vulnerability, with optional Artifact evidence."""

    @vulnerability_group.command("report")
    @click.option("--title", required=True)
    @click.option("--type", "vulnerability_type", required=True)
    @click.option(
        "--severity",
        type=click.Choice(("critical", "high", "medium", "low", "info")),
        required=True,
    )
    @click.option("--description")
    @click.option("--description-file", type=click.Path(path_type=Path, dir_okay=False))
    @click.option("--prerequisites")
    @click.option("--prerequisites-file", type=click.Path(path_type=Path, dir_okay=False))
    @click.option("--impact")
    @click.option("--impact-file", type=click.Path(path_type=Path, dir_okay=False))
    @click.option("--remediation")
    @click.option("--remediation-file", type=click.Path(path_type=Path, dir_okay=False))
    @click.option("--evidence-path", multiple=True)
    @click.option("--affected-target", default="")
    def explore_vulnerability_report(
        title: str,
        vulnerability_type: str,
        severity: str,
        description: str | None,
        description_file: Path | None,
        prerequisites: str | None,
        prerequisites_file: Path | None,
        impact: str | None,
        impact_file: Path | None,
        remediation: str | None,
        remediation_file: Path | None,
        evidence_path: tuple[str, ...],
        affected_target: str,
    ) -> None:
        report = {
            "title": title,
            "type": vulnerability_type,
            "severity": severity,
            "description": _text_value(
                description,
                description_file,
                "description",
                required=True,
            ),
            "prerequisites": _text_value(
                prerequisites,
                prerequisites_file,
                "prerequisites",
                required=True,
            ),
            "impact": _text_value(impact, impact_file, "impact", required=True),
            "remediation": _text_value(
                remediation,
                remediation_file,
                "remediation",
                required=True,
            ),
            "evidence_paths": list(evidence_path),
            "affected_target": affected_target,
        }
        task_id = _environment("AXIOM_TASK_ID")
        intent_id = _environment("AXIOM_INTENT_ID")
        path = f"/dispatch/tasks/{task_id}/intents/{intent_id}/explore-vulnerabilities"
        _echo(_request("POST", path, {"lease_id": _lease_id(), "report": report}))

    @explore_group.group("human-intervention")
    def explore_human_intervention_group() -> None:
        """Mark a direction that requires a real human action to continue."""

    @explore_human_intervention_group.command("mark")
    @click.option("--reason")
    @click.option("--reason-file", type=click.Path(path_type=Path, dir_okay=False))
    def explore_human_intervention_mark(
        reason: str | None,
        reason_file: Path | None,
    ) -> None:
        task_id = _environment("AXIOM_TASK_ID")
        intent_id = _environment("AXIOM_INTENT_ID")
        path = f"/dispatch/tasks/{task_id}/intents/{intent_id}/human-intervention"
        _echo(
            _request(
                "POST",
                path,
                {
                    "lease_id": _lease_id(),
                    "reason": _text_value(reason, reason_file, "reason", required=True),
                },
            )
        )

    @main.group("review")
    def review_group() -> None:
        """Review-stage operations for the active Vulnerability ReviewRun."""

    @review_group.command("status")
    def review_status() -> None:
        _echo(_review_request("status", {"lease_id": _lease_id()}))

    @review_group.group("decision")
    def review_decision_group() -> None:
        """Submit the Reviewer decision without changing vulnerability content."""

    @review_decision_group.command("submit")
    @click.argument(
        "decision",
        type=click.Choice(("verified", "duplicate", "ignored", "needs-human")),
    )
    @click.option("--reason")
    @click.option("--reason-file", type=click.Path(path_type=Path, dir_okay=False))
    @click.option("--duplicate-of")
    def review_decision_submit(
        decision: str,
        reason: str | None,
        reason_file: Path | None,
        duplicate_of: str | None,
    ) -> None:
        duplicate_of = (duplicate_of or "").strip() or None
        if decision == "duplicate" and not duplicate_of:
            raise click.UsageError("duplicate requires --duplicate-of vXXX")
        if decision != "duplicate" and duplicate_of:
            raise click.UsageError("--duplicate-of is only valid for duplicate")
        _echo(
            _review_request(
                "decision",
                {
                    "lease_id": _lease_id(),
                    "decision": decision,
                    "reason": _text_value(reason, reason_file, "reason", required=True),
                    "duplicate_of": duplicate_of,
                },
            )
        )


def _workspace_request(method: str, action: str, payload: dict[str, Any]) -> Any:
    _environment("AXIOM_REASON_RUN_ID")
    workspace_id = _environment("AXIOM_REASON_WORKSPACE_ID")
    return _request(
        method,
        f"/reason-workspaces/{workspace_id}/{action}",
        {"lease_id": _lease_id(), **payload},
    )


def _plan_workspace_request(action: str, payload: dict[str, Any]) -> Any:
    _environment("AXIOM_PLAN_RUN_ID")
    workspace_id = _environment("AXIOM_PLAN_WORKSPACE_ID")
    return _request(
        "POST",
        f"/plan-workspaces/{workspace_id}/{action}",
        {"lease_id": _lease_id(), **payload},
    )


def _review_request(action: str, payload: dict[str, Any]) -> Any:
    task_id = _environment("AXIOM_TASK_ID")
    review_run_id = _environment("AXIOM_REVIEW_RUN_ID")
    return _request(
        "POST",
        f"/dispatch/tasks/{task_id}/vulnerability-review-runs/{review_run_id}/{action}",
        payload,
    )


def _request(method: str, path: str, payload: dict[str, Any]) -> Any:
    base_url = os.environ.get("AXIOM_SERVER", "http://127.0.0.1:8899").rstrip("/")
    if not base_url.endswith("/api/v1"):
        base_url += "/api/v1"
    headers = {"content-type": "application/json"}
    token = os.environ.get("AXIOM_SERVICE_TOKEN", "").strip()
    if token:
        headers["authorization"] = f"Bearer {token}"
    try:
        with httpx.Client(base_url=base_url, headers=headers, timeout=20, trust_env=False) as client:
            response = client.request(method, path, json=payload)
    except httpx.HTTPError as exc:
        raise click.ClickException(f"AXIOM API unavailable: {exc}") from exc
    if response.is_error:
        try:
            body = response.json()
            detail = body.get("error", {}).get("message") or body.get("detail") or response.text
        except (ValueError, AttributeError):
            detail = response.text
        if not isinstance(detail, str):
            detail = json.dumps(detail, ensure_ascii=False)
        raise click.ClickException(f"AXIOM API HTTP {response.status_code}: {detail}")
    return response.json() if response.content else None


def _environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise click.ClickException(f"missing required environment variable: {name}")
    return value


def _lease_id() -> str:
    return _environment("AXIOM_LEASE_ID")


def _credential_access_payload() -> dict[str, str]:
    intent_id = os.environ.get("AXIOM_INTENT_ID", "").strip()
    reason_run_id = os.environ.get("AXIOM_REASON_RUN_ID", "").strip()
    plan_run_id = os.environ.get("AXIOM_PLAN_RUN_ID", "").strip()
    review_run_id = os.environ.get("AXIOM_REVIEW_RUN_ID", "").strip()
    owners = [
        ("intent_id", intent_id),
        ("reason_run_id", reason_run_id),
        ("plan_run_id", plan_run_id),
        ("review_run_id", review_run_id),
    ]
    selected = [(key, value) for key, value in owners if value]
    if len(selected) != 1:
        raise click.ClickException(
            "credential access requires exactly one active Intent, ReasonRun, PlanRun or ReviewRun"
        )
    payload = {"lease_id": _lease_id()}
    owner_key, owner_value = selected[0]
    payload[owner_key] = owner_value
    return payload


def _credential_markdown(value: dict[str, Any]) -> str:
    name = value.get("name")
    content = value.get("content")
    if not isinstance(name, str) or not name.strip():
        raise click.ClickException("AXIOM API credential is missing its name")
    if not isinstance(content, str) or not content.strip():
        raise click.ClickException("AXIOM API credential is missing its content")

    lines = [
        "# 项目凭证",
        "",
        f"- ID: {value.get('id', '')}",
        f"- 名称: {name}",
    ]
    refresh_id = str(value.get("refresh_id") or "").strip()
    if refresh_id:
        lines.append(f"- 凭证刷新 ID: {refresh_id}")
    notes = value.get("notes")
    if isinstance(notes, str) and notes.strip():
        lines.extend(["", "## 备注", "", notes])
    lines.extend(["", "## 凭证内容", "", content])
    return "\n".join(lines)


def _write_private_text(output: Path, content: str) -> Path:
    run_dir = Path(_environment("AXIOM_RUN_DIR")).expanduser().resolve()
    requested = output.expanduser()
    if not requested.is_absolute():
        requested = run_dir / requested
    parent = requested.parent.resolve()
    destination = parent / requested.name
    if not parent.is_relative_to(run_dir):
        raise click.ClickException("credential output must stay inside AXIOM_RUN_DIR")
    if destination.is_symlink():
        raise click.ClickException("credential output must not be a symlink")
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        dir=parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            if content and not content.endswith("\n"):
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        temporary.replace(destination)
        os.chmod(destination, 0o600)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return destination


def _source(value: str) -> dict[str, str]:
    kind, separator, item_id = value.strip().partition(":")
    if not separator or kind not in SOURCE_TYPES or not item_id:
        raise click.BadParameter(f"invalid source ref: {value}")
    return {"type": kind, "id": item_id}


def _text_value(
    value: str | None,
    path: Path | None,
    label: str,
    *,
    required: bool = False,
) -> str:
    if value is not None and path is not None:
        raise click.UsageError(f"use either --{label.replace(' ', '-')} or its --*-file option")
    if path is not None:
        try:
            value = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise click.ClickException(f"cannot read {label} file: {exc}") from exc
    normalized = (value or "").strip()
    if required and not normalized:
        raise click.UsageError(f"{label} is required")
    return normalized


def _set_optional_text(
    target: dict[str, Any],
    key: str,
    value: str | None,
    path: Path | None,
) -> None:
    if value is None and path is None:
        return
    target[key] = _text_value(value, path, key.replace("_", " "))


def _echo(value: Any) -> None:
    click.echo(json.dumps(value, ensure_ascii=False, indent=2))


def _echo_workspace(value: dict[str, Any]) -> None:
    draft = value.get("draft", {})
    _echo(
        {
            "workspace_id": value.get("id"),
            "status": value.get("status"),
            "todo_add_count": len(draft.get("todo_adds", [])),
            "todo_update_count": len(draft.get("todo_updates", [])),
            "intent_proposal_count": len(draft.get("intent_proposals", [])),
            "intent_assessment_count": len(draft.get("intent_assessments", [])),
            "goal_assessed": draft.get("goal_assessment") is not None,
        }
    )


def _echo_plan_workspace(value: dict[str, Any]) -> None:
    draft = value.get("draft", {})
    _echo(
        {
            "workspace_id": value.get("id"),
            "status": value.get("status"),
            "todo_add_count": len(draft.get("todo_adds", [])),
            "todo_update_count": len(draft.get("todo_updates", [])),
            "intent_proposal_count": len(draft.get("intent_proposals", [])),
            "decision": draft.get("decision"),
        }
    )
