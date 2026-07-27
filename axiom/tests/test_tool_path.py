from __future__ import annotations

import os
from pathlib import Path

from axiom.dispatcher.tool_path import build_tool_path, prepare_runtime_bin


def test_tool_path_prefers_operator_and_system_tools_over_virtualenv(
    tmp_path: Path,
) -> None:
    configured = tmp_path / "configured"
    runtime = tmp_path / "runtime"
    virtualenv = tmp_path / ".venv" / "bin"

    result = build_tool_path(
        inherited=os.pathsep.join([str(virtualenv), "/usr/bin", "/usr/local/bin"]),
        configured=str(configured),
        runtime_bin=runtime,
        fallback_bins=[virtualenv],
        home=tmp_path,
    ).split(os.pathsep)

    assert result[:4] == [
        str(runtime),
        str(configured),
        "/usr/local/bin",
        "/opt/homebrew/bin",
    ]
    assert result.index("/usr/local/bin") < result.index(str(virtualenv))
    assert result.count("/usr/local/bin") == 1


def test_tool_path_discovers_browser_and_workbuddy_bins(tmp_path: Path) -> None:
    browser_bin = tmp_path / "browser-use-env" / "bin"
    older_node = tmp_path / ".workbuddy/binaries/node/versions/20.1.0/bin"
    newer_node = tmp_path / ".workbuddy/binaries/node/versions/22.10.2/bin"
    for path in (browser_bin, older_node, newer_node):
        path.mkdir(parents=True)

    result = build_tool_path(inherited="/usr/bin", home=tmp_path).split(os.pathsep)

    assert result.index(str(browser_bin)) < result.index("/usr/bin")
    assert result.index(str(newer_node)) < result.index(str(older_node))


def test_runtime_bin_exposes_only_controlled_python_and_axiom(tmp_path: Path) -> None:
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    python = venv_bin / "python3.14"
    axiom = venv_bin / "axiom"
    httpx = venv_bin / "httpx"
    for path in (python, axiom, httpx):
        path.write_text("#!/bin/sh\n", encoding="utf-8")

    runtime = prepare_runtime_bin(
        tmp_path / "run-bin",
        python_executable=python,
        axiom_executable=axiom,
    )

    assert str(python.absolute()) in (runtime / "python").read_text(encoding="utf-8")
    assert str(python.absolute()) in (runtime / "python3").read_text(encoding="utf-8")
    assert str(axiom.absolute()) in (runtime / "axiom").read_text(encoding="utf-8")
    assert os.access(runtime / "python3", os.X_OK)
    assert not (runtime / "httpx").exists()
