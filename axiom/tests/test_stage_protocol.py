from __future__ import annotations

from pathlib import Path

import pytest

from axiom.modules.execution.stage_outputs import (
    StageOutputError,
    bootstrap_files,
    explore_files,
    require_finish,
)


@pytest.mark.parametrize("value", ["FINISH", " FINISH\n"])
def test_finish_marker_accepts_only_trimmed_exact_finish(value: str) -> None:
    require_finish(value)


@pytest.mark.parametrize(
    "value",
    ["", "finish", "FINISH.", "```\nFINISH\n```", "完成\nFINISH", "FINISH\n说明"],
)
def test_finish_marker_rejects_explanations_code_blocks_and_other_text(value: str) -> None:
    with pytest.raises(StageOutputError, match="FINISH"):
        require_finish(value)


def test_bootstrap_collects_only_the_three_fixed_outputs(tmp_path: Path) -> None:
    for filename in ("asset_inventory.md", "initial_leads.md", "findings.md"):
        (tmp_path / filename).write_text(f"# {filename}\n", encoding="utf-8")
    (tmp_path / "models.json").write_text('{"secret":"ignored"}', encoding="utf-8")
    (tmp_path / "auth.json").write_text('{"credential":"ignored"}', encoding="utf-8")
    (tmp_path / "sessions").mkdir()
    (tmp_path / "sessions" / "session.jsonl").write_text("ignored", encoding="utf-8")
    (tmp_path / "process").mkdir()
    (tmp_path / "process" / "stdout.log").write_text("ignored", encoding="utf-8")

    outputs = bootstrap_files(tmp_path)

    assert [item.relative_name for item in outputs] == [
        "asset_inventory.md",
        "initial_leads.md",
        "findings.md",
    ]


def test_explore_collects_only_artifacts_directory(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "evidence.md").write_text("# 中文证据\n", encoding="utf-8")
    (tmp_path / "models.json").write_text('{"secret":"ignored"}', encoding="utf-8")
    (tmp_path / "auth.json").write_text('{"credential":"ignored"}', encoding="utf-8")
    (tmp_path / "sessions").mkdir()
    (tmp_path / "sessions" / "session.jsonl").write_text("ignored", encoding="utf-8")
    (tmp_path / "process").mkdir()
    (tmp_path / "process" / "stdout.log").write_text("ignored", encoding="utf-8")

    outputs = explore_files(tmp_path)

    assert [item.relative_name for item in outputs] == ["evidence.md"]


@pytest.mark.parametrize("relative_name", ["models.json", "sessions/output.md", "process/log.md"])
def test_explore_rejects_internal_runtime_files_inside_artifacts(
    tmp_path: Path,
    relative_name: str,
) -> None:
    path = tmp_path / "artifacts" / relative_name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("不应注册", encoding="utf-8")

    with pytest.raises(StageOutputError, match="禁止"):
        explore_files(tmp_path)


def test_explore_rejects_symlinks_that_escape_the_artifacts_directory(tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("外部内容", encoding="utf-8")
    artifacts = tmp_path / "run" / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "evidence.md").symlink_to(outside)

    with pytest.raises(StageOutputError, match="符号链接"):
        explore_files(tmp_path / "run")


def test_explore_requires_at_least_one_non_empty_artifact(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "empty.md").touch()

    with pytest.raises(StageOutputError, match="为空"):
        explore_files(tmp_path)
