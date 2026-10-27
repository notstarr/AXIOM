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


def test_bootstrap_registers_evidence_from_both_evidence_dirs(tmp_path: Path) -> None:
    for filename in ("asset_inventory.md", "initial_leads.md"):
        (tmp_path / filename).write_text(f"# {filename}\n", encoding="utf-8")
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "baseline-api.txt").write_text("HTTP/1.1 200 OK\n", encoding="utf-8")
    (evidence / "empty.txt").write_text("", encoding="utf-8")
    nested = evidence / "sub"
    nested.mkdir()
    (nested / "debug.json").write_text('{"a":1}', encoding="utf-8")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "evidence").mkdir()
    (artifacts / "evidence" / "auth.txt").write_text("authorization: Bearer x\n", encoding="utf-8")
    # 与 evidence/ 同名时只登记一次
    (artifacts / "evidence" / "baseline-api.txt").write_text("dup\n", encoding="utf-8")

    outputs = bootstrap_files(tmp_path)

    evidence_outputs = [item for item in outputs if item.artifact_type == "bootstrap.evidence"]
    assert [item.relative_name for item in evidence_outputs] == [
        "evidence/auth.txt",
        "evidence/baseline-api.txt",
        "evidence/sub/debug.json",
    ]
    assert all(item.summary.startswith("Bootstrap 证据：") for item in evidence_outputs)


def test_bootstrap_skips_placeholder_extra_files(tmp_path: Path) -> None:
    for filename in ("asset_inventory.md", "initial_leads.md"):
        (tmp_path / filename).write_text(f"# {filename}\n", encoding="utf-8")
    (tmp_path / "mcp-auth.json").write_text("{}", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("real\n", encoding="utf-8")

    outputs = bootstrap_files(tmp_path)

    names = [item.relative_name for item in outputs]
    assert "mcp-auth.json" not in names
    assert "notes.txt" in names


def test_explore_registers_only_the_two_conclusion_files(tmp_path: Path) -> None:
    """Explore's file channel is now just findings.md + leads.md.

    The proof travels as structured reproduction (verbatim packets) submitted through
    the CLI, so a leftover artifacts/evidence/ directory is neither collected nor
    registered — it stays on disk with the run directory and gets a warning.
    """
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "findings.md").write_text("# 已确认事实\n\n本轮无新增确认事实\n", encoding="utf-8")
    (artifacts / "leads.md").write_text("# 待验证线索\n\n本轮无遗留待验证线索\n", encoding="utf-8")
    evidence = artifacts / "evidence"
    evidence.mkdir()
    (evidence / "response.txt").write_text("HTTP/1.1 200 OK\n", encoding="utf-8")
    work = artifacts / "work"
    work.mkdir()
    (work / "probe_log.txt").write_text("GET / -> 500\n", encoding="utf-8")
    (tmp_path / "models.json").write_text('{"secret":"ignored"}', encoding="utf-8")
    (tmp_path / "auth.json").write_text('{"credential":"ignored"}', encoding="utf-8")
    (tmp_path / "sessions").mkdir()
    (tmp_path / "sessions" / "session.jsonl").write_text("ignored", encoding="utf-8")

    outputs = explore_files(tmp_path)

    assert [item.relative_name for item in outputs] == ["findings.md", "leads.md"]
    assert [item.artifact_type for item in outputs] == ["explore.findings", "explore.leads"]
    assert all(
        item.artifact_type not in {"explore.evidence", "explore.work"} for item in outputs
    )


def test_explore_warns_once_about_retired_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker still following the old instructions gets told, not silently ignored."""
    artifacts = tmp_path / "artifacts"
    _explore_core(artifacts)
    (artifacts / "evidence").mkdir()
    (artifacts / "evidence" / "proof.txt").write_text("proof", encoding="utf-8")
    (artifacts / "work").mkdir()
    (artifacts / "work" / "probe_log.txt").write_text("log", encoding="utf-8")

    warnings: list[str] = []
    monkeypatch.setattr(
        "axiom.modules.execution.stage_outputs.logger.warning",
        lambda message, *args: warnings.append(message % args),
    )

    outputs = explore_files(tmp_path)

    assert [item.relative_name for item in outputs] == ["findings.md", "leads.md"]
    assert any("reason=retired_directory" in message for message in warnings)
    assert any("evidence" in message and "work" in message for message in warnings)


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


def test_explore_registers_root_jsfinder_results(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "findings.md").write_text("# 已确认事实\n\n本轮无新增确认事实\n", encoding="utf-8")
    (artifacts / "leads.md").write_text("# 待验证线索\n\n本轮无遗留待验证线索\n", encoding="utf-8")
    (tmp_path / "jsfinder_result-www-a-cn-ab12.json").write_text('{"tool":"jsfinder"}', encoding="utf-8")
    (tmp_path / "jsfinder_result-empty-cd34.json").write_text("", encoding="utf-8")
    (tmp_path / "models.json").write_text('{"secret":"ignored"}', encoding="utf-8")

    outputs = explore_files(tmp_path)

    jsfinder = [item for item in outputs if item.artifact_type == "explore.jsfinder"]
    assert [item.relative_name for item in jsfinder] == ["jsfinder_result-www-a-cn-ab12.json"]
    assert jsfinder[0].summary.startswith("Explore JSFinder 能力树")


def _explore_core(artifacts: Path) -> None:
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "findings.md").write_text("# 已确认事实\n\n本轮无新增确认事实\n", encoding="utf-8")
    (artifacts / "leads.md").write_text("# 待验证线索\n\n本轮无遗留待验证线索\n", encoding="utf-8")


def test_work_records_are_left_on_disk_but_not_registered(tmp_path: Path) -> None:
    """Original records stay with the run directory: not evidence, not an artifact.

    They used to be registered so a human could inspect the probing matrix afterwards;
    that is now what the reproduction steps and the run directory are for, and
    registering them competed with the evidence channel that no longer exists.
    """
    artifacts = tmp_path / "artifacts"
    _explore_core(artifacts)
    work = artifacts / "work"
    work.mkdir()
    (work / "probe_log.txt").write_text("GET / -> 500\n" * 3, encoding="utf-8")
    nested = work / "scripts"
    nested.mkdir()
    (nested / "harvest.py").write_text("print('x')\n", encoding="utf-8")

    outputs = explore_files(tmp_path)

    assert [item.artifact_type for item in outputs] == ["explore.findings", "explore.leads"]
    # The files themselves are untouched — pruning is the sandbox's job, not ours.
    assert (work / "probe_log.txt").is_file()
    assert (nested / "harvest.py").is_file()


def test_bootstrap_registers_work_records(tmp_path: Path) -> None:
    for filename in ("asset_inventory.md", "initial_leads.md"):
        (tmp_path / filename).write_text(f"# {filename}\n", encoding="utf-8")
    work = tmp_path / "artifacts" / "work"
    work.mkdir(parents=True)
    (work / "scan_matrix.txt").write_text("a b c\n", encoding="utf-8")

    outputs = bootstrap_files(tmp_path)

    work_outputs = [item for item in outputs if item.artifact_type == "bootstrap.work"]
    assert [item.relative_name for item in work_outputs] == ["work/scan_matrix.txt"]


def test_explore_without_work_directory_is_unchanged(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    _explore_core(artifacts)

    outputs = explore_files(tmp_path)

    assert [item.artifact_type for item in outputs] == ["explore.findings", "explore.leads"]
