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
