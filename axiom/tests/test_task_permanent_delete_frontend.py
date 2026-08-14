from __future__ import annotations

from pathlib import Path


STATIC_ROOT = Path(__file__).parents[1] / "src" / "axiom" / "static"


def test_permanent_delete_is_only_offered_for_archived_tasks() -> None:
    task_list = (STATIC_ROOT / "templates" / "task-list.html").read_text(encoding="utf-8")
    assert "selectedTaskSummary().archived" in task_list
    assert ">永久删除</button>" in task_list

    for template_name in ("archive-view.html", "human-view.html", "graph-view.html"):
        content = (STATIC_ROOT / "templates" / template_name).read_text(encoding="utf-8")
        assert "deleteProjectById(project.project.id, project.project.title)" in content
        assert "永久删除" in content


def test_permanent_delete_confirmation_lists_purged_data() -> None:
    index = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
    assert "永久删除任务" in index
    assert "Fact、Todo、Intent、漏洞、证据文件、运行目录和 Token 统计" in index
