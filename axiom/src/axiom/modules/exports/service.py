from __future__ import annotations

import io
import json
import logging
import zipfile
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import Engine, select

from axiom.modules.artifacts.repository import ArtifactRepository
from axiom.modules.audit.service import AuditService
from axiom.modules.goals.repository import GoalRepository
from axiom.modules.graph.repository import GraphRepository
from axiom.modules.knowledge.tables import project_knowledge
from axiom.modules.projects.repository import ProjectRepository
from axiom.modules.tasks.repository import TaskRepository
from axiom.modules.todos.repository import TodoRepository
from axiom.modules.reports import ReportEvidenceRepository
from axiom.modules.redteam.repository import RedTeamRepository
from axiom.modules.reports.skeleton import (
    build_result_report,
    build_vulnerability_report,
    to_markdown,
)
from axiom.modules.vulnerabilities.repository import VulnerabilityRepository
from axiom.shared.errors import NotFoundError
from axiom.shared.storage import FileStore


logger = logging.getLogger(__name__)


class ExportService:
    """Build stable human exports without exposing credentials or audit noise."""

    def __init__(self, engine: Engine, store: FileStore | None = None):
        self.engine = engine
        self.store = store or FileStore(Path.home() / ".local/share/axiom-v3")
        self.tasks = TaskRepository()
        self.projects = ProjectRepository()
        self.graph = GraphRepository()
        self.artifacts = ArtifactRepository()
        self.todos = TodoRepository()
        self.vulnerabilities = VulnerabilityRepository()
        self.report_evidence = ReportEvidenceRepository()
        self.goals = GoalRepository()
        self.audit = AuditService(engine)

    def _images(self, conn, task_id: str, subject_type: str, subject_id: str) -> list[dict]:
        """The pictures attached to this report, named for a self-contained folder.

        Filenames carry the step number and the kind so the folder sorts in reading order
        and a reader can tell the packet shot (the response on the wire) from the business
        shot (the effect).  The markdown references exactly the name written into the
        archive — a mismatch here is a broken report that only shows up after delivery.
        """
        items = self.report_evidence.list(conn, task_id, subject_type, subject_id)
        artifacts = {item.id: item for item in self.artifacts.list(conn, task_id)}
        collected: list[dict] = []
        used: dict[str, int] = {}
        for item in items:
            artifact = artifacts.get(item["artifact_id"])
            if artifact is None:
                continue
            path = self.store.resolve(artifact.storage_key)
            if not path.is_file():
                logger.warning(
                    "report_image_missing task_id=%s subject=%s artifact=%s key=%s",
                    task_id, subject_id, item["artifact_id"], artifact.storage_key,
                )
                continue
            step = int(item.get("step_index") or 0)
            kind = item.get("kind") or "business"
            name = path.name
            # 上传时人可能已经按步骤命名（step-02-id.png），再叠一层前缀会变成
            # step-02-step-02-id.png。已经带了本步编号就原样用——名字里缺种类无妨，
            # 图片的先后顺序由这里决定，不靠文件名。
            prefix = f"step-{step:02d}-"
            key = name if name.startswith(prefix) else f"{prefix}{kind}-{name}"
            if key in used:
                used[key] += 1
                stem, dot, ext = key.rpartition(".")
                key = f"{stem}-{used[key]}{dot}{ext}" if dot else f"{key}-{used[key]}"
            else:
                used[key] = 1
            collected.append(
                {
                    "step": step,
                    "kind": kind,
                    "path": f"images/{key}",
                    "caption": item.get("caption") or "",
                    "file": path,
                }
            )
        return collected

    @staticmethod
    def _images_by_step(images: list[dict]) -> dict[int, list[dict]]:
        grouped: dict[int, list[dict]] = {}
        for image in images:
            grouped.setdefault(image["step"], []).append(image)
        return grouped

    def export_vulnerability_report(
        self, task_id: str, vulnerability_id: str
    ) -> tuple[bytes, str, str]:
        """交付报告：md +（有图时）图片，返回 (bytes, filename, media_type)。

        No images means no archive: wrapping a single markdown file in a zip makes the
        recipient unzip to read one file.
        """
        self._require_task(task_id)
        with self.engine.connect() as conn:
            vuln = self.vulnerabilities.get(conn, task_id, vulnerability_id)
            if vuln is None:
                raise NotFoundError(f"Vulnerability not found: {vulnerability_id}")
            images = self._images(conn, task_id, "vulnerability", vulnerability_id)

        markdown = self.vulnerability_markdown(task_id, vulnerability_id)
        return self._package(task_id, vulnerability_id, markdown, images)

    def export_result_report(
        self, task_id: str, result_id: str
    ) -> tuple[bytes, str, str]:
        self._require_task(task_id)
        with self.engine.connect() as conn:
            result = RedTeamRepository().get_result(conn, task_id, result_id)
            if result is None:
                raise NotFoundError(f"RedTeam Result not found: {result_id}")
            images = self._images(conn, task_id, "redteam_result", result_id)

        markdown = self.result_markdown(task_id, result_id)
        return self._package(task_id, result_id, markdown, images)

    def _package(
        self, task_id: str, subject_id: str, markdown: str, images: list[dict]
    ) -> tuple[bytes, str, str]:
        base = f"{task_id}_{subject_id}_report"
        if not images:
            return markdown.encode("utf-8"), f"{base}.md", "text/markdown"
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("report.md", markdown)
            for image in images:
                archive.writestr(image["path"], image["file"].read_bytes())
        return buffer.getvalue(), f"{base}.zip", "application/zip"

    def overview_yaml(self, task_id: str) -> str:
        data = self._overview(task_id)
        return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)

    def overview_json(self, task_id: str) -> str:
        return json.dumps(self._overview(task_id), ensure_ascii=False, indent=2) + "\n"

    def timeline(self, task_id: str) -> str:
        self._require_task(task_id)
        events = self.audit.list(task_id=task_id, limit=2_000)
        lines = [f"任务时间线：{task_id}", "=" * 60]
        for event in reversed(events):
            actor = event.worker or event.event_type
            phase = str(event.payload.get("phase") or "")
            prefix = f"[{event.created_at}] {phase + ' · ' if phase else ''}{actor}"
            lines.append(f"{prefix}：{event.message}")
        if len(lines) == 2:
            lines.append("暂无执行事件。")
        return "\n".join(lines) + "\n"

    def vulnerability_markdown(self, task_id: str, vulnerability_id: str) -> str:
        """交付用 Markdown：由代码从结构化复现生成，不经 AI 重写。

        Images are referenced by the same ``images/…`` paths the archive writes, so the
        markdown and the folder are one unit.  With no images the paths simply do not
        resolve, which is why the package drops the archive in that case.
        """
        self._require_task(task_id)
        with self.engine.connect() as conn:
            item = self.vulnerabilities.get(conn, task_id, vulnerability_id)
            if item is None:
                raise NotFoundError(f"Vulnerability not found: {vulnerability_id}")
            images = self._images(conn, task_id, "vulnerability", vulnerability_id)
        return self._render_markdown(item, images)

    def result_markdown(self, task_id: str, result_id: str) -> str:
        self._require_task(task_id)
        with self.engine.connect() as conn:
            result = RedTeamRepository().get_result(conn, task_id, result_id)
            if result is None:
                raise NotFoundError(f"RedTeam Result not found: {result_id}")
            images = self._images(conn, task_id, "redteam_result", result_id)
        doc = build_result_report(result, images_by_step=self._images_by_step(images))
        return to_markdown(doc)

    def _render_markdown(self, item, images: list[dict]) -> str:
        if item.reproduction is None:
            return _vulnerability_markdown(item.model_dump(mode="json"))
        doc = build_vulnerability_report(
            item, images_by_step=self._images_by_step(images)
        )
        return to_markdown(doc)

    def vulnerabilities_markdown(self, task_id: str) -> str:
        self._require_task(task_id)
        with self.engine.connect() as conn:
            items = self.vulnerabilities.list(conn, task_id)
            evidence_by_id = self.report_evidence.for_subjects(
                conn,
                task_id,
                "vulnerability",
                [item.id for item in items],
            )
        if not items:
            return f"# 漏洞报告\n\n任务：{task_id}\n\n当前没有漏洞记录。\n"
        blocks = [f"# 漏洞报告\n\n任务：{task_id}\n"]
        blocks.extend(
            self._render_markdown(item, evidence_by_id.get(item.id, []))
            for item in items
        )
        return "\n---\n\n".join(blocks)

    def _overview(self, task_id: str) -> dict[str, Any]:
        with self.engine.connect() as conn:
            task = self.tasks.get(conn, task_id)
            if task is None:
                raise NotFoundError(f"Task not found: {task_id}")
            project = self.projects.get(conn, task.project_id)
            graph = self.graph.graph(conn, task_id)
            todos = self.todos.list(conn, task_id)
            artifacts = self.artifacts.list(conn, task_id)
            vulnerabilities = self.vulnerabilities.list(conn, task_id)
            goals = self.goals.list(conn, task_id)
            goal_cycles = self.goals.list_cycles(conn, task_id)
            knowledge = [
                {
                    "id": row["id"],
                    "title": row["title"],
                    "category": row["category"],
                    "content": row["content"],
                }
                for row in conn.execute(
                    select(project_knowledge)
                    .where(
                        project_knowledge.c.project_id == task.project_id,
                        project_knowledge.c.archived.is_(False),
                    )
                    .order_by(project_knowledge.c.created_at, project_knowledge.c.id)
                ).mappings()
            ]
        return {
            "概况": {
                "task_id": task.id,
                "title": task.title,
                "project_id": task.project_id,
                "project_title": project.title if project else "",
                "origin": task.origin.model_dump(mode="json"),
                "goal": task.goal,
                "status": task.status,
                "phase": task.phase,
                "scope": task.scope.model_dump(mode="json"),
            },
            "facts": [item.model_dump(mode="json") for item in graph.facts],
            "intents": [item.model_dump(mode="json") for item in graph.intents],
            "todos": [item.model_dump(mode="json") for item in todos],
            "artifacts": [
                {
                    "id": item.id,
                    "intent_id": item.intent_id,
                    "type": item.artifact_type,
                    "storage_key": item.storage_key,
                    "summary": item.summary,
                    "size": item.size,
                    "created_by": item.created_by,
                    "created_at": item.created_at,
                }
                for item in artifacts
            ],
            "vulnerabilities": [item.model_dump(mode="json") for item in vulnerabilities],
            "goal_evaluations": [item.model_dump(mode="json") for item in goals],
            "goal_cycles": [item.model_dump(mode="json") for item in goal_cycles],
            "knowledge": knowledge,
            "hints": [item.model_dump(mode="json") for item in self._hints(task_id)],
        }

    def _hints(self, task_id: str):
        with self.engine.connect() as conn:
            return self.tasks.hints(conn, task_id)

    def _require_task(self, task_id: str):
        with self.engine.connect() as conn:
            task = self.tasks.get(conn, task_id)
        if task is None:
            raise NotFoundError(f"Task not found: {task_id}")
        return task


def _render_vulnerability_markdown(item, evidence: list[dict[str, Any]]) -> str:
    """The delivered Markdown for one vulnerability.

    Content comes from the structured reproduction; layout comes from the skeleton.
    Historical rows with no reproduction fall back to the legacy renderer below so an
    old export keeps its prose instead of becoming an empty template.
    """
    if item.reproduction is None:
        return _vulnerability_markdown(item.model_dump(mode="json"))
    return to_markdown(build_vulnerability_report(item, evidence=evidence))


def _vulnerability_markdown(item: dict[str, Any]) -> str:
    def bullets(values: list[str]) -> str:
        return "\n".join(f"- {value}" for value in values) if values else "- 无"

    finding = item.get("finding_artifact") or {}
    evidence_artifacts = item.get("evidence_artifacts") or []
    finding_text = (
        f"- `artifact:{finding['id']}` · `{finding['storage_key']}`"
        if finding
        else "- 未关联"
    )
    evidence_text = bullets(
        [f"`artifact:{artifact['id']}` · `{artifact['storage_key']}`" for artifact in evidence_artifacts]
    )
    legacy = item.get("legacy") or None
    legacy_section = ""
    if legacy is not None:
        legacy_section = f"""

## 历史格式内容（只读）

### 旧复现步骤

{bullets(legacy.get('reproduction_steps') or [])}

### 旧文字证据

{bullets(legacy.get('evidence') or [])}

### 旧请求示例

```text
{legacy.get('curl_command') or '未提供'}
```
"""

    return f"""# {item['title']}

- ID：`{item['id']}`
- 类型：{item['type']}
- 严重性：{item['severity']}
- 状态：{item['status']}
- 影响目标：{item.get('affected_target') or '未填写'}

## 描述

{item['description']}

## 前置条件

{item.get('prerequisites') or '无'}

## 影响

{item['impact']}

## 关联 Findings

{finding_text}

## 证据文件

{evidence_text}

## 修复建议

{item.get('remediation') or '未填写'}
{legacy_section}"""
