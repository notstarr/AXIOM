from __future__ import annotations

import io
import json
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
from axiom.modules.vulnerabilities.repository import VulnerabilityRepository
from axiom.shared.errors import NotFoundError
from axiom.shared.storage import FileStore


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
        self.goals = GoalRepository()
        self.audit = AuditService(engine)

    def export_vulnerability_zip(self, task_id: str, vulnerability_id: str) -> tuple[bytes, str]:
        """打包导出单漏洞原文 (vulnerability.md)、原始 Findings (findings.md) 以及所有关联 Evidence 附件。"""
        self._require_task(task_id)
        with self.engine.connect() as conn:
            vuln = self.vulnerabilities.get(conn, task_id, vulnerability_id)
            if vuln is None:
                raise NotFoundError(f"Vulnerability not found: {vulnerability_id}")

            intent_id = vuln.intent_id
            artifacts_list = self.artifacts.list(conn, task_id)

        vuln_md = _vulnerability_markdown(vuln.model_dump(mode="json"))

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
            # 1. 写入漏洞原文
            zip_file.writestr("vulnerability.md", vuln_md)

            # 2. 查找并写入原始 findings.md 与 evidence 附件
            intent_artifacts = [a for a in artifacts_list if a.intent_id == intent_id] if intent_id else []
            
            findings_written = False
            for art in intent_artifacts:
                art_path = self.store.resolve(art.storage_key)
                if not art_path.is_file():
                    continue

                storage_key = art.storage_key.replace("\\", "/")
                # 写入 findings.md
                if storage_key.endswith("findings.md") and not findings_written:
                    zip_file.writestr("findings.md", art_path.read_bytes())
                    findings_written = True
                # 写入 evidence/ 下的附件
                elif "/evidence/" in storage_key:
                    rel_name = storage_key.split("/evidence/")[-1]
                    zip_file.writestr(f"evidence/{rel_name}", art_path.read_bytes())
                elif art.artifact_type.startswith("explore.evidence"):
                    zip_file.writestr(f"evidence/{art_path.name}", art_path.read_bytes())

            if not findings_written:
                zip_file.writestr("findings.md", "# Findings\n\n暂无关联的原始 Findings 记录。\n")

        zip_filename = f"{task_id}_{vulnerability_id}_export.zip"
        return zip_buffer.getvalue(), zip_filename

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
        self._require_task(task_id)
        with self.engine.connect() as conn:
            item = self.vulnerabilities.get(conn, task_id, vulnerability_id)
        if item is None:
            raise NotFoundError(f"Vulnerability not found: {vulnerability_id}")
        return _vulnerability_markdown(item.model_dump(mode="json"))

    def vulnerabilities_markdown(self, task_id: str) -> str:
        self._require_task(task_id)
        with self.engine.connect() as conn:
            items = self.vulnerabilities.list(conn, task_id)
        if not items:
            return f"# 漏洞报告\n\n任务：{task_id}\n\n当前没有漏洞记录。\n"
        blocks = [f"# 漏洞报告\n\n任务：{task_id}\n"]
        blocks.extend(_vulnerability_markdown(item.model_dump(mode="json")) for item in items)
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
