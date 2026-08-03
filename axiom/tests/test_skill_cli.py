from __future__ import annotations

import json
import os
from pathlib import Path

from click.testing import CliRunner

from axiom.cli import main


def _skill(root: Path, name: str, frontmatter: str = "") -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    directory.joinpath("SKILL.md").write_text(
        f"---\nname: {name}\n{frontmatter}---\n\n# {name}\n",
        encoding="utf-8",
    )
    return directory


def test_skill_path_resolves_an_active_skill_without_using_cwd(tmp_path: Path) -> None:
    skill = _skill(tmp_path, "browser-control")

    result = CliRunner().invoke(
        main,
        ["skill", "path", "browser-control"],
        env={"AXIOM_SKILL_DIRECTORIES": str(skill)},
    )

    assert result.exit_code == 0, result.output
    assert result.output.strip() == str(skill.resolve())


def test_skill_doctor_checks_command_probe_module_and_file(tmp_path: Path) -> None:
    command_bin = tmp_path / "bin"
    command_bin.mkdir()
    command = command_bin / "probe-tool"
    command.write_text("#!/bin/sh\necho 'expected identity'\n", encoding="utf-8")
    command.chmod(0o755)
    skill = _skill(
        tmp_path / "skills",
        "probe",
        "requirements:\n"
        "  commands:\n"
        "    - name: probe-tool\n"
        "      args: [--version]\n"
        "      contains: expected identity\n"
        "  python_modules: [json]\n"
        "  files: [scripts/helper.py]\n",
    )
    helper = skill / "scripts" / "helper.py"
    helper.parent.mkdir()
    helper.write_text("", encoding="utf-8")

    result = CliRunner().invoke(
        main,
        ["skill", "doctor", "probe"],
        env={
            "AXIOM_SKILL_DIRECTORIES": str(skill),
            "PATH": os.pathsep.join([str(command_bin), os.environ.get("PATH", "")]),
        },
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["skills"][0]["checks"][3]["path"] == str(command)


def test_skill_doctor_fails_when_a_required_tool_is_missing(tmp_path: Path) -> None:
    skill = _skill(
        tmp_path,
        "missing",
        "requirements:\n  commands: [definitely-not-an-axiom-command]\n",
    )

    result = CliRunner().invoke(
        main,
        ["skill", "doctor", "missing"],
        env={"AXIOM_SKILL_DIRECTORIES": str(skill), "PATH": "/usr/bin:/bin"},
    )

    assert result.exit_code == 1
    assert json.loads(result.output)["ok"] is False


def test_skill_doctor_runs_functional_probe_with_skill_placeholder(
    tmp_path: Path,
) -> None:
    skill = _skill(
        tmp_path,
        "functional",
        "requirements:\n"
        "  probes:\n"
        "    - name: functional-read\n"
        "      command: [python3, '{skill}/scripts/probe.py']\n"
        "      contains: functional-ok\n",
    )
    probe = skill / "scripts" / "probe.py"
    probe.parent.mkdir()
    probe.write_text("print('functional-ok')\n", encoding="utf-8")

    result = CliRunner().invoke(
        main,
        ["skill", "doctor", "functional"],
        env={"AXIOM_SKILL_DIRECTORIES": str(skill)},
    )

    assert result.exit_code == 0, result.output
    check = json.loads(result.output)["skills"][0]["checks"][-1]
    assert check["kind"] == "probe"
    assert check["name"] == "functional-read"
    assert check["ok"] is True


def test_skill_doctor_reports_functional_probe_timeout(tmp_path: Path) -> None:
    skill = _skill(
        tmp_path,
        "slow",
        "requirements:\n"
        "  probes:\n"
        "    - name: slow-probe\n"
        "      command: [python3, '-c', 'import time; time.sleep(1)']\n"
        "      timeout_seconds: 0.1\n",
    )

    result = CliRunner().invoke(
        main,
        ["skill", "doctor", "slow"],
        env={"AXIOM_SKILL_DIRECTORIES": str(skill)},
    )

    assert result.exit_code == 1
    check = json.loads(result.output)["skills"][0]["checks"][-1]
    assert check["ok"] is False
    assert "timed out" in check["error"]
