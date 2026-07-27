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
TODO_STATUSES = ("pending", "active", "completed", "rejected", "obsolete")
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
        destination = _write_private_json(output, credential)
        data = credential.get("data") if isinstance(credential, dict) else {}
        _echo(
            {
                "id": credential_id,
                "output": str(destination),
                "data_fields": sorted(str(key) for key in data) if isinstance(data, dict) else [],
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
    @click.option("--status", type=click.Choice(TODO_STATUSES), default="pending")
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

    @reason_group.group("intent")
    def reason_intent_group() -> None:
        """Propose one pending Explore Intent."""

    @reason_intent_group.command("propose")
    @click.option("--todo-ref")
    @click.option("--todo-id")
    @click.option("--from-fact", multiple=True, required=True)
    @click.option("--description")
    @click.option("--description-file", type=click.Path(path_type=Path, dir_okay=False))
    @click.option("--target", multiple=True, required=True)
    @click.option("--execute-seconds", type=click.IntRange(1), required=True)
    @click.option("--conclude-seconds", type=click.IntRange(1), required=True)
    def reason_intent_propose(
        todo_ref: str | None,
        todo_id: str | None,
        from_fact: tuple[str, ...],
        description: str | None,
        description_file: Path | None,
        target: tuple[str, ...],
        execute_seconds: int,
        conclude_seconds: int,
    ) -> None:
        if bool(todo_ref) == bool(todo_id):
            raise click.UsageError("exactly one of --todo-ref or --todo-id is required")
        proposal = {
            "todo_ref": todo_ref,
            "todo_id": todo_id,
            "from_facts": list(from_fact),
            "description": _text_value(
                description,
                description_file,
                "description",
                required=True,
            ),
            "targets": list(target),
            "execute_seconds": execute_seconds,
            "conclude_seconds": conclude_seconds,
        }
        _echo_workspace(_workspace_request("POST", "intents", {"intent": proposal}))

    @reason_group.group("goal")
    def reason_goal_group() -> None:
        """Assess whether the Task Goal has been achieved."""

    @reason_goal_group.command("assess")
    @click.option("--achieved/--not-achieved", default=None)
    @click.option("--reason", "assessment_reason")
    @click.option("--reason-file", type=click.Path(path_type=Path, dir_okay=False))
    @click.option("--evidence", multiple=True)
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

    @main.group("explore")
    def explore_group() -> None:
        """Explore-stage code-maintained operations."""

    @explore_group.group("vulnerability")
    def vulnerability_group() -> None:
        """Report one evidence-backed vulnerability."""

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
    @click.option("--reproduction-step", multiple=True)
    @click.option("--remediation")
    @click.option("--remediation-file", type=click.Path(path_type=Path, dir_okay=False))
    @click.option("--curl-command")
    @click.option("--curl-command-file", type=click.Path(path_type=Path, dir_okay=False))
    @click.option("--evidence", multiple=True)
    @click.option("--affected-target", default="")
    @click.option(
        "--status",
        type=click.Choice(("待验证", "已验证", "忽略", "重复")),
        default="待验证",
    )
    @click.option("--status-reason", default="")
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
        reproduction_step: tuple[str, ...],
        remediation: str | None,
        remediation_file: Path | None,
        curl_command: str | None,
        curl_command_file: Path | None,
        evidence: tuple[str, ...],
        affected_target: str,
        status: str,
        status_reason: str,
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
            ),
            "impact": _text_value(impact, impact_file, "impact", required=True),
            "reproduction_steps": list(reproduction_step),
            "remediation": _text_value(remediation, remediation_file, "remediation"),
            "curl_command": _text_value(
                curl_command,
                curl_command_file,
                "curl command",
            ),
            "evidence": list(evidence),
            "affected_target": affected_target,
            "status": status,
            "status_reason": status_reason,
        }
        task_id = _environment("AXIOM_TASK_ID")
        intent_id = _environment("AXIOM_INTENT_ID")
        path = f"/dispatch/tasks/{task_id}/intents/{intent_id}/explore-vulnerabilities"
        _echo(_request("POST", path, {"lease_id": _lease_id(), "report": report}))


def _workspace_request(method: str, action: str, payload: dict[str, Any]) -> Any:
    _environment("AXIOM_REASON_RUN_ID")
    workspace_id = _environment("AXIOM_REASON_WORKSPACE_ID")
    return _request(
        method,
        f"/reason-workspaces/{workspace_id}/{action}",
        {"lease_id": _lease_id(), **payload},
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
    if bool(intent_id) == bool(reason_run_id):
        raise click.ClickException(
            "credential access requires exactly one of AXIOM_INTENT_ID or AXIOM_REASON_RUN_ID"
        )
    payload = {"lease_id": _lease_id()}
    payload["intent_id" if intent_id else "reason_run_id"] = intent_id or reason_run_id
    return payload


def _write_private_json(output: Path, value: Any) -> Path:
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
            json.dump(value, handle, ensure_ascii=False, indent=2)
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
            "goal_assessed": draft.get("goal_assessment") is not None,
        }
    )
