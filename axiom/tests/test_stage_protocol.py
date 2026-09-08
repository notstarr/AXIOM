from __future__ import annotations

from pathlib import Path

import pytest

from axiom.modules.execution.stage_outputs import (
    StageOutputError,
    bootstrap_files,
    explore_files,
    require_explore_finish,
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


@pytest.mark.parametrize(
    ("value", "normalization"),
    [
        ("FINISH", None),
        (" FINISH\n", None),
        ("`FINISH`", "inline_code"),
        ("```\nFINISH\n```", "code_fence"),
        ("```text\nFINISH\n```", "code_fence"),
        ("检查完成。\nFINISH", "trailing_line"),
        ("检查完成。\n`FINISH`", "trailing_inline_code"),
        ("检查完成。\n```text\nFINISH\n```", "trailing_code_fence"),
    ],
)
def test_explore_finish_marker_accepts_safe_terminal_variants(
    value: str,
    normalization: str | None,
) -> None:
    assert require_explore_finish(value) == normalization


@pytest.mark.parametrize(
    "value",
    [
        "",
        "finish",
        "FINISH。",
        "abc FINISH",
        "FINISH\n仍有说明",
        "```\nFINISH。\n```",
        "检查完成。\n```\nFINISH\n```\n仍有说明",
    ],
)
def test_explore_finish_marker_rejects_non_terminal_or_changed_markers(value: str) -> None:
    with pytest.raises(StageOutputError, match="FINISH"):
        require_explore_finish(value)


def test_bootstrap_collects_required_outputs_and_optional_jsfinder(tmp_path: Path) -> None:
    for filename in ("asset_inventory.md", "initial_leads.md", "findings.md"):
        (tmp_path / filename).write_text(f"# {filename}\n", encoding="utf-8")
    (tmp_path / "jsfinder_result.json").write_text('{"tool":"jsfinder"}', encoding="utf-8")
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
        "jsfinder_result.json",
    ]
    assert [item.artifact_type for item in outputs][-1] == "bootstrap.jsfinder"


def test_bootstrap_ignores_missing_or_invalid_jsfinder_output(tmp_path: Path) -> None:
    for filename in ("asset_inventory.md", "initial_leads.md"):
        (tmp_path / filename).write_text(f"# {filename}\n", encoding="utf-8")
    (tmp_path / "jsfinder_result.json").write_text("", encoding="utf-8")

    outputs = bootstrap_files(tmp_path)

    assert [item.relative_name for item in outputs] == [
        "asset_inventory.md",
        "initial_leads.md",
    ]


def test_explore_collects_only_artifacts_directory(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "findings.md").write_text("# 已确认事实\n\n本轮无新增确认事实\n", encoding="utf-8")
    (artifacts / "leads.md").write_text("# 待验证线索\n\n本轮无遗留待验证线索\n", encoding="utf-8")
    evidence = artifacts / "evidence"
    evidence.mkdir()
    (evidence / "response.txt").write_text("HTTP/1.1 200 OK\n", encoding="utf-8")
    (tmp_path / "models.json").write_text('{"secret":"ignored"}', encoding="utf-8")
    (tmp_path / "auth.json").write_text('{"credential":"ignored"}', encoding="utf-8")
    (tmp_path / "sessions").mkdir()
    (tmp_path / "sessions" / "session.jsonl").write_text("ignored", encoding="utf-8")
    (tmp_path / "process").mkdir()
    (tmp_path / "process" / "stdout.log").write_text("ignored", encoding="utf-8")

    outputs = explore_files(tmp_path)

    assert [item.relative_name for item in outputs] == [
        "findings.md",
        "leads.md",
        "evidence/response.txt",
    ]
    assert [item.artifact_type for item in outputs] == [
        "explore.findings",
        "explore.leads",
        "explore.evidence",
    ]


@pytest.mark.parametrize("relative_name", ["models.json", "sessions/output.md", "process/log.md"])
def test_explore_ignores_unknown_root_entries_inside_artifacts(
    tmp_path: Path,
    relative_name: str,
) -> None:
    path = tmp_path / "artifacts" / relative_name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("不应注册", encoding="utf-8")
    (tmp_path / "artifacts" / "findings.md").write_text("已确认事实", encoding="utf-8")
    (tmp_path / "artifacts" / "leads.md").write_text("待验证线索", encoding="utf-8")

    outputs = explore_files(tmp_path)

    assert [item.relative_name for item in outputs] == ["findings.md", "leads.md"]


def test_explore_skips_symlinks_that_escape_the_artifacts_directory(tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("外部内容", encoding="utf-8")
    artifacts = tmp_path / "run" / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "findings.md").write_text("已确认事实", encoding="utf-8")
    (artifacts / "leads.md").write_text("待验证线索", encoding="utf-8")
    (artifacts / "evidence").mkdir()
    (artifacts / "evidence" / "response.md").symlink_to(outside)

    outputs = explore_files(tmp_path / "run")

    assert [item.relative_name for item in outputs] == ["findings.md", "leads.md"]


def test_explore_requires_both_non_empty_core_artifacts(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "findings.md").touch()
    (artifacts / "leads.md").write_text("本轮无遗留待验证线索", encoding="utf-8")

    with pytest.raises(StageOutputError, match="为空"):
        explore_files(tmp_path)


def test_explore_allows_no_evidence_when_no_vulnerability_was_reported(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "findings.md").write_text("本轮无新增确认事实", encoding="utf-8")
    (artifacts / "leads.md").write_text("本轮无遗留待验证线索", encoding="utf-8")

    outputs = explore_files(tmp_path)

    assert [item.relative_name for item in outputs] == ["findings.md", "leads.md"]


@pytest.mark.parametrize("directory", ["analysis", "reproduction", "other"])
def test_explore_ignores_old_or_unknown_directories(tmp_path: Path, directory: str) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "findings.md").write_text("已确认事实", encoding="utf-8")
    (artifacts / "leads.md").write_text("待验证线索", encoding="utf-8")
    (artifacts / directory).mkdir()
    (artifacts / directory / "output.txt").write_text("旧目录", encoding="utf-8")

    outputs = explore_files(tmp_path)

    assert [item.relative_name for item in outputs] == ["findings.md", "leads.md"]


def test_explore_skips_empty_and_unsafe_evidence_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = tmp_path / "artifacts"
    evidence = artifacts / "evidence"
    evidence.mkdir(parents=True)
    (artifacts / "findings.md").write_text("已确认事实", encoding="utf-8")
    (artifacts / "leads.md").write_text("待验证线索", encoding="utf-8")
    (evidence / "empty.log").touch()
    (evidence / "unsafe=name.txt").write_text("ignored", encoding="utf-8")
    (evidence / "response.txt").write_text("HTTP/1.1 200 OK", encoding="utf-8")

    warnings: list[tuple[str, tuple[object, ...]]] = []
    monkeypatch.setattr(
        "axiom.modules.execution.stage_outputs.logger.warning",
        lambda message, *args: warnings.append((message, args)),
    )

    outputs = explore_files(tmp_path)

    assert [item.relative_name for item in outputs] == [
        "findings.md",
        "leads.md",
        "evidence/response.txt",
    ]
    rendered = [message % args for message, args in warnings]
    assert any("reason=empty count=1" in message for message in rendered)
    assert any("reason=unsafe_path count=1" in message for message in rendered)
    assert any("evidence_registered=1 skipped=2" in message for message in rendered)


def test_explore_registers_at_most_48_optional_evidence_files(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    evidence = artifacts / "evidence"
    evidence.mkdir(parents=True)
    (artifacts / "findings.md").write_text("已确认事实", encoding="utf-8")
    (artifacts / "leads.md").write_text("待验证线索", encoding="utf-8")
    for number in range(60):
        (evidence / f"proof-{number:02d}.txt").write_text(
            f"proof {number}",
            encoding="utf-8",
        )

    outputs = explore_files(tmp_path)

    assert len(outputs) == 50
    assert outputs[2].relative_name == "evidence/proof-00.txt"
    assert outputs[-1].relative_name == "evidence/proof-47.txt"
