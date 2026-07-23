from __future__ import annotations

import os
from pathlib import Path

from axiom.dispatcher.tool_path import build_tool_path


def test_tool_path_prefers_operator_and_system_tools_over_virtualenv(tmp_path: Path) -> None:
    configured = tmp_path / "configured"
    virtualenv = tmp_path / ".venv" / "bin"

    result = build_tool_path(
        inherited=os.pathsep.join([str(virtualenv), "/usr/bin", "/usr/local/bin"]),
        configured=str(configured),
        home=tmp_path,
    ).split(os.pathsep)

    assert result[:3] == [str(configured), "/usr/local/bin", "/opt/homebrew/bin"]
    assert result.index("/usr/local/bin") < result.index(str(virtualenv))
    assert result.count("/usr/local/bin") == 1
