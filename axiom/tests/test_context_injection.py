from __future__ import annotations

import json
from pathlib import Path

from axiom.modules.context.injection import ContextInjectionService
from axiom.modules.context.schemas import ContextRevision


def _revision(tmp_path: Path) -> ContextRevision:
    root = tmp_path / "context"
    (root / "details" / "intent").mkdir(parents=True)
    (root / "manifest.json").write_text(
        json.dumps({"revision": 7, "phase": "explore", "indexes": {"hints": "hints.json"}}),
        encoding="utf-8",
    )
    (root / "hints.json").write_text(
        json.dumps([{"id": "h001", "content": "优先验证入口"}], ensure_ascii=False),
        encoding="utf-8",
    )
    return ContextRevision(
        task_id="t001",
        revision=7,
        phase="explore",
        intent_id="i001",
        manifest_key="contexts/t001/r000007/manifest.json",
        manifest_path=str(root / "manifest.json"),
        content_hash="context-hash",
        created_at="2026-09-07T00:00:00Z",
    )


def test_injection_is_delimited_hashed_and_keeps_manifest_refs(tmp_path: Path) -> None:
    injected = ContextInjectionService().build(
        revision=_revision(tmp_path),
        phase="explore",
        intent_id="i001",
        current_description="验证对象级授权",
        current_targets=["https://target.example"],
    )

    assert injected.source_revision == 7
    assert injected.content_hash
    assert "BEGIN AXIOM INJECTED CONTEXT" in injected.text
    assert "验证对象级授权" in injected.text
    assert "h001" in injected.text
    assert "hints.json" in injected.source_refs
    assert injected.estimated_tokens > 0


def test_injection_renders_catalog_cards_and_drops_removed_indexes(tmp_path: Path) -> None:
    root = tmp_path / "context"
    (root / "details" / "intent").mkdir(parents=True)
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "revision": 9,
                "phase": "reason",
                "indexes": {
                    "hints": "hints.json",
                    "knowledge": "knowledge.json",
                    "artifacts": "artifacts.json",
                    "vulnerabilities": "vulnerabilities.json",
                    "todos": "todos.json",
                    "graph": "graph.json",
                },
            }
        ),
        encoding="utf-8",
    )
    (root / "hints.json").write_text("[]", encoding="utf-8")
    (root / "knowledge.json").write_text("[]", encoding="utf-8")
    (root / "artifacts.json").write_text(
        json.dumps(
            [
                {"id": "a001", "type": "explore.findings", "summary": "事实"},
                {"id": "a002", "type": "explore.leads", "summary": "线索"},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    # Even large removed-index payloads must never leak into the prompt body.
    big_rows = [{"id": f"v{i:03d}", "title": "漏" * 200} for i in range(50)]
    (root / "todos.json").write_text("[]", encoding="utf-8")
    (root / "graph.json").write_text(json.dumps({"facts": []}), encoding="utf-8")
    revision = ContextRevision(
        task_id="t001",
        revision=9,
        phase="reason",
        intent_id=None,
        manifest_key="contexts/t001/r000009/manifest.json",
        manifest_path=str(root / "manifest.json"),
        content_hash="context-hash",
        created_at="2026-09-09T00:00:00Z",
    )

    injected = ContextInjectionService().build(revision=revision, phase="reason")

    # Catalog cards replace full index bodies.
    assert "共 2 行" in injected.text and "a001-a002" in injected.text
    # Removed indexes: no row bodies, no titles in the prompt body.
    assert "漏" * 200 not in injected.text
    assert "## vulnerabilities" not in injected.text
    assert "## todos" not in injected.text
    assert "## graph" not in injected.text
    assert "whatisvul" not in injected.text
    # Catalog cards stay tiny: whole block far below the 24K-token budget.
    assert injected.estimated_tokens < 2_000
