from __future__ import annotations

from axiom.dispatcher.prompting import load_prompt


def test_bootstrap_prompts_make_single_bundle_submission_bounded_and_explicit() -> None:
    execute = load_prompt("v2", "bootstrap.md")
    conclude = load_prompt("v2", "bootstrap_conclude.md")

    assert "bootstrap_artifacts.json" in execute
    assert '"asset_inventory"' in execute
    assert '"initial_leads"' in execute
    assert '"findings"' in execute
    assert "Do not call `axiom-tool artifact register`" in execute
    assert "Do not edit the AXIOM database directly" in execute
    assert "phase_context" in execute
    assert "axiom-tool context get" in execute
    assert "Do not perform new network exploration" in conclude
    assert "Do not edit the AXIOM database directly" in conclude
    assert "{run_dir}/asset_inventory.md" in conclude
    assert "bootstrap_artifacts.json" in conclude
    assert "no registration command is required" in conclude
    assert "axiom-tool artifact register" not in conclude


def test_explore_prompt_contains_exact_artifact_commands() -> None:
    prompt = load_prompt("v2", "explore.md")
    conclude = load_prompt("v2", "explore_conclude.md")

    assert "axiom-tool artifact register --type findings" in prompt
    assert "axiom-tool artifact register --type leads" in prompt
    assert '"accepted": true' in prompt
    assert '"description"' in prompt
    assert "findings.md" in conclude
    assert "leads.md" in conclude
    assert '"accepted": true' in conclude
    assert "Do not output JSON" not in conclude
    assert "Do not edit the AXIOM database directly" in prompt
    assert "phase_context" in prompt
    assert "do not chase unrelated global-index entries" in prompt
    assert "keep them updated throughout the run" in prompt
    assert "Do not postpone" in prompt
    assert "A description that only says" in prompt
    assert "leads.md” is invalid" in prompt
    assert "{evidence_path}" in conclude
    assert "authoritative" in conclude
    assert "Do not claim that a tool" in conclude
    assert "only points to the two files is invalid" in conclude


def test_reason_prompt_uses_delta_and_on_demand_expansion() -> None:
    prompt = load_prompt("v2", "reason.md")

    assert "complete delta since the previous committed Reason round" in prompt
    assert "axiom-tool context get" in prompt
    assert "axiom-tool context related" in prompt
    assert "preview text is never complete evidence" in prompt
    assert "严禁把预算" in prompt
    assert "结构化字段" in prompt
    assert "{intent_budget}" in prompt
    assert "{open_intent_count}" in prompt


def test_all_v2_phase_prompts_require_chinese_human_readable_output() -> None:
    for filename in (
        "bootstrap.md",
        "bootstrap_conclude.md",
        "explore.md",
        "explore_conclude.md",
        "reason.md",
    ):
        prompt = load_prompt("v2", filename)
        assert "All human-readable" in prompt, filename
        assert "Chinese" in prompt, filename
