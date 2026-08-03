from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click
import yaml

from axiom.dispatcher.tool_path import discover_tool_environment


_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


@dataclass(frozen=True, slots=True)
class CommandRequirement:
    name: str
    args: tuple[str, ...] = ()
    contains: str = ""


@dataclass(frozen=True, slots=True)
class ProbeRequirement:
    name: str
    command: tuple[str, ...]
    contains: str = ""
    timeout_seconds: float = 8.0


def register_skill_commands(main: click.Group) -> None:
    @main.group("skill")
    def skill_group() -> None:
        """Resolve and validate Skills available to this Worker lease."""

    @skill_group.command("path")
    @click.argument("name")
    def skill_path(name: str) -> None:
        """Print the absolute directory of one active Skill."""

        click.echo(str(resolve_skill_directory(name, _environment_directories())))

    @skill_group.command("doctor")
    @click.argument("names", nargs=-1)
    def skill_doctor(names: tuple[str, ...]) -> None:
        """Check active Skill files, commands, Python modules, and probes."""

        directories = _environment_directories()
        roots = (
            [resolve_skill_directory(name, directories) for name in names]
            if names
            else active_skill_directories(directories)
        )
        environment = os.environ.copy()
        environment.update(discover_tool_environment(environment))
        results = [_doctor_skill(root, environment=environment) for root in roots]
        payload = {
            "ok": all(result["ok"] for result in results),
            "skills": results,
            "summary": {
                "checked": len(results),
                "passed": sum(bool(result["ok"]) for result in results),
                "failed": sum(not bool(result["ok"]) for result in results),
            },
        }
        click.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        if not payload["ok"]:
            raise click.exceptions.Exit(1)


def active_skill_directories(directories: list[Path]) -> list[Path]:
    result: list[Path] = []
    for directory in directories:
        candidates = (
            [directory]
            if (directory / "SKILL.md").is_file()
            else directory.glob("*/SKILL.md")
        )
        for candidate in candidates:
            root = candidate.parent if candidate.name == "SKILL.md" else candidate
            root = root.resolve()
            if (root / "SKILL.md").is_file() and root not in result:
                result.append(root)
    return result


def resolve_skill_directory(name: str, directories: list[Path]) -> Path:
    name = name.strip()
    if not _SAFE_NAME.fullmatch(name):
        raise click.ClickException("invalid Skill name")
    for directory in directories:
        candidates = [directory] if directory.name == name else [directory / name]
        for candidate in candidates:
            candidate = candidate.resolve()
            if candidate.name == name and (candidate / "SKILL.md").is_file():
                return candidate
    raise click.ClickException(f"active Skill not found: {name}")


def _environment_directories() -> list[Path]:
    value = os.environ.get("AXIOM_SKILL_DIRECTORIES", "").strip()
    if not value:
        raise click.ClickException(
            "missing required environment variable: AXIOM_SKILL_DIRECTORIES"
        )
    return [
        Path(part).expanduser().resolve() for part in value.split(os.pathsep) if part
    ]


def _doctor_skill(
    root: Path, *, environment: dict[str, str] | None = None
) -> dict[str, Any]:
    environment = environment or os.environ.copy()
    metadata = _frontmatter(root / "SKILL.md")
    requirements = metadata.get("requirements") or {}
    if not isinstance(requirements, dict):
        requirements = {}
    checks: list[dict[str, Any]] = [
        {"kind": "file", "name": "SKILL.md", "ok": True, "path": str(root / "SKILL.md")}
    ]
    for relative in _strings(requirements.get("files")):
        path = _skill_file(root, relative)
        checks.append(
            {"kind": "file", "name": relative, "ok": path.is_file(), "path": str(path)}
        )
    for module in _strings(requirements.get("python_modules")):
        checks.append(
            {
                "kind": "python_module",
                "name": module,
                "ok": importlib.util.find_spec(module) is not None,
            }
        )
    for requirement in _commands(requirements.get("commands")):
        checks.append(
            _check_command(requirement, optional=False, environment=environment)
        )
    for requirement in _commands(requirements.get("optional_commands")):
        checks.append(
            _check_command(requirement, optional=True, environment=environment)
        )
    for requirement in _probes(requirements.get("probes")):
        checks.append(_check_probe(root, requirement, environment=environment))
    required_checks = [check for check in checks if not check.get("optional")]
    return {
        "name": str(metadata.get("name") or root.name),
        "path": str(root),
        "ok": all(bool(check["ok"]) for check in required_checks),
        "checks": checks,
    }


def _check_command(
    requirement: CommandRequirement,
    *,
    optional: bool,
    environment: dict[str, str],
) -> dict[str, Any]:
    resolved = shutil.which(requirement.name, path=environment.get("PATH"))
    check: dict[str, Any] = {
        "kind": "command",
        "name": requirement.name,
        "ok": resolved is not None,
        "path": resolved or "",
        "optional": optional,
    }
    if resolved is None or not requirement.args:
        return check
    try:
        process = subprocess.run(
            [resolved, *requirement.args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
            env=environment,
        )
        output = process.stdout + process.stderr
        check["returncode"] = process.returncode
        check["ok"] = process.returncode == 0 and (
            not requirement.contains
            or requirement.contains.casefold() in output.casefold()
        )
        if not check["ok"]:
            check["error"] = _one_line(output) or f"exit {process.returncode}"
    except (OSError, subprocess.TimeoutExpired) as exc:
        check["ok"] = False
        check["error"] = str(exc)
    return check


def _check_probe(
    root: Path,
    requirement: ProbeRequirement,
    *,
    environment: dict[str, str],
) -> dict[str, Any]:
    command = [part.replace("{skill}", str(root)) for part in requirement.command]
    check: dict[str, Any] = {
        "kind": "probe",
        "name": requirement.name,
        "command": command,
        "ok": False,
    }
    if not command:
        check["error"] = "empty probe command"
        return check

    executable = command[0]
    if not Path(executable).is_absolute():
        resolved = shutil.which(executable, path=environment.get("PATH"))
        if resolved is None:
            check["error"] = f"command not found: {executable}"
            return check
        command[0] = resolved
        check["command"] = command
    try:
        process = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=requirement.timeout_seconds,
            check=False,
            cwd=root,
            env=environment,
        )
        output = process.stdout + process.stderr
        check["returncode"] = process.returncode
        check["ok"] = process.returncode == 0 and (
            not requirement.contains
            or requirement.contains.casefold() in output.casefold()
        )
        if not check["ok"]:
            check["error"] = _one_line(output) or f"exit {process.returncode}"
    except subprocess.TimeoutExpired:
        check["error"] = f"probe timed out after {requirement.timeout_seconds:g}s"
    except OSError as exc:
        check["error"] = str(exc)
    return check


def _frontmatter(path: Path) -> dict[str, Any]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if not lines or lines[0].strip() != "---":
        return {"name": path.parent.name}
    try:
        end = next(
            index for index, line in enumerate(lines[1:], 1) if line.strip() == "---"
        )
    except StopIteration:
        return {"name": path.parent.name}
    parsed = yaml.safe_load("\n".join(lines[1:end]))
    return parsed if isinstance(parsed, dict) else {"name": path.parent.name}


def _commands(value: Any) -> list[CommandRequirement]:
    values = value if isinstance(value, list) else []
    result: list[CommandRequirement] = []
    for item in values:
        if isinstance(item, str) and item.strip():
            result.append(CommandRequirement(item.strip()))
        elif isinstance(item, dict) and str(item.get("name") or "").strip():
            result.append(
                CommandRequirement(
                    str(item["name"]).strip(),
                    tuple(_strings(item.get("args"))),
                    str(item.get("contains") or "").strip(),
                )
            )
    return result


def _probes(value: Any) -> list[ProbeRequirement]:
    values = value if isinstance(value, list) else []
    result: list[ProbeRequirement] = []
    for item in values:
        if not isinstance(item, dict):
            continue
        command = tuple(_strings(item.get("command")))
        if not command:
            continue
        raw_timeout = item.get("timeout_seconds", 8)
        try:
            timeout_seconds = float(raw_timeout)
        except (TypeError, ValueError):
            timeout_seconds = 8.0
        result.append(
            ProbeRequirement(
                name=str(item.get("name") or command[0]).strip(),
                command=command,
                contains=str(item.get("contains") or "").strip(),
                timeout_seconds=min(max(timeout_seconds, 0.1), 60.0),
            )
        )
    return result


def _strings(value: Any) -> list[str]:
    return [str(item).strip() for item in value] if isinstance(value, list) else []


def _skill_file(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not relative or Path(relative).is_absolute() or not path.is_relative_to(root):
        raise click.ClickException(f"invalid Skill requirement path: {relative}")
    return path


def _one_line(value: str) -> str:
    return " ".join(value.strip().split())[:300]
