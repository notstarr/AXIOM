from __future__ import annotations

import os
from pathlib import Path


def build_tool_path(
    *,
    inherited: str | None = None,
    configured: str | None = None,
    home: Path | None = None,
) -> str:
    """Put operator-installed security tools ahead of Python virtualenv shims.

    AXIOM itself normally runs from a virtualenv.  Some Python packages expose
    command names also used by security tools (notably ``httpx``), so blindly
    forwarding the Runner's PATH can launch the wrong program.  Operators may
    override precedence with ``AXIOM_TOOL_PATH``; common installation roots are
    then preferred before the inherited PATH.
    """

    user_home = home or Path.home()
    candidates = [
        *_split(configured),
        "/usr/local/bin",
        "/opt/homebrew/bin",
        str(user_home / "go" / "bin"),
        str(user_home / ".local" / "bin"),
        *_split(inherited if inherited is not None else os.environ.get("PATH", "")),
    ]
    return os.pathsep.join(dict.fromkeys(path for path in candidates if path))


def _split(value: str | None) -> list[str]:
    return [part for part in (value or "").split(os.pathsep) if part]
