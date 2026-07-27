from __future__ import annotations

import os
import re
import shlex
from pathlib import Path
from typing import Iterable


def build_tool_path(
    *,
    inherited: str | None = None,
    configured: str | None = None,
    runtime_bin: str | Path | None = None,
    fallback_bins: Iterable[str | Path] = (),
    home: Path | None = None,
) -> str:
    """Build the deterministic PATH inherited by a Worker process.

    ``runtime_bin`` contains only AXIOM's controlled ``axiom``/``python``
    launchers.  Security tools are then resolved from the operator override and
    common installation roots before falling back to the virtualenv and the
    Runner's inherited PATH.  This avoids Python package console scripts (most
    notably ``httpx``) shadowing the actual security tool while keeping AXIOM's
    own interpreter deterministic.
    """

    user_home = home or Path.home()
    candidates = [
        *_parts(runtime_bin),
        *_split(configured),
        "/usr/local/bin",
        "/opt/homebrew/bin",
        "/usr/local/go/bin",
        str(user_home / "go" / "bin"),
        str(user_home / ".local" / "bin"),
        str(user_home / ".cargo" / "bin"),
        *_discovered_tool_bins(user_home),
        *(str(path) for path in fallback_bins),
        *_split(inherited if inherited is not None else os.environ.get("PATH", "")),
    ]
    return os.pathsep.join(dict.fromkeys(path for path in candidates if path))


def prepare_runtime_bin(
    root: Path,
    *,
    python_executable: str | Path,
    axiom_executable: str | Path | None = None,
) -> Path:
    """Create a tiny controlled command directory for one Worker run.

    The full virtualenv must not lead PATH because it may contain console-script
    name collisions.  Only the commands whose identity AXIOM must control are
    exposed here.  Entries are replaced atomically so a reused run directory
    cannot retain stale interpreter links.
    """

    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    python_target = Path(python_executable).expanduser().absolute()
    axiom_target = (
        Path(axiom_executable).expanduser().absolute()
        if axiom_executable is not None
        else python_target.parent / "axiom"
    )
    commands = {"python": python_target, "python3": python_target}
    if axiom_target.is_file():
        commands["axiom"] = axiom_target
    for name, target in commands.items():
        _replace_launcher(root / name, target)
    return root


def _split(value: str | None) -> list[str]:
    return [part for part in (value or "").split(os.pathsep) if part]


def _parts(value: str | Path | None) -> list[str]:
    return [] if value is None else _split(str(value))


def _discovered_tool_bins(home: Path) -> list[str]:
    """Return installation roots used by supported local integrations."""

    result: list[Path] = []
    browser_tools = home / "browser-use-env" / "bin"
    if browser_tools.is_dir():
        result.append(browser_tools)

    node_versions = home / ".workbuddy" / "binaries" / "node" / "versions"
    if node_versions.is_dir():
        result.extend(
            sorted(
                (path for path in node_versions.glob("*/bin") if path.is_dir()),
                key=_versioned_bin_key,
                reverse=True,
            )
        )
    return [str(path) for path in result]


def _versioned_bin_key(path: Path) -> tuple[int, ...]:
    numbers = tuple(int(value) for value in re.findall(r"\d+", path.parent.name))
    return numbers or (0,)


def _replace_launcher(path: Path, target: Path) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        temporary.unlink(missing_ok=True)
        temporary.write_text(
            f'#!/bin/sh\nexec {shlex.quote(str(target))} "$@"\n',
            encoding="utf-8",
        )
        temporary.chmod(0o700)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
