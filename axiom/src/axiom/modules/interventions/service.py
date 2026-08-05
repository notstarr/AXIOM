from __future__ import annotations

import json
import re
from typing import Any
from uuid import uuid4

from sqlalchemy import Engine, select

from axiom.modules.audit.tables import audit_events
from axiom.modules.graph.tables import intents
from axiom.modules.interventions.repository import InterventionRepository
from axiom.modules.interventions.schemas import CreateIntervention, Intervention, ResolveIntervention
from axiom.modules.tasks.tables import tasks
from axiom.shared.clock import utcnow
from axiom.shared.database import transaction
from axiom.shared.errors import ConflictError, NotFoundError
from axiom.shared.storage import FileStore


_SENSITIVE = re.compile(
    r"(?i)([\"']?(?:authorization|cookie|api[-_ ]?key|access[-_ ]?token|token|password|passwd|secret|jwt)"
    r"[\"']?\s*[:=]\s*(?:bearer\s+)?)(?:\"[^\"]*\"|'[^']*'|[^\s,;}\]\"']+)"
)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9._-]{10,}")
_CLI_SENSITIVE = re.compile(
    r"(?i)(--?(?:api[-_]?key|access[-_]?token|token|password|passwd|secret|authorization|cookie|jwt)"
    r"(?:=|\s+))(?:(?:\"[^\"]*\")|(?:'[^']*')|\S+)"
)
_SENSITIVE_KEY = re.compile(
    r"(?i)(?:authorization|cookie|api[-_ ]?key|access[-_ ]?token|token|password|passwd|secret|jwt)"
)


class InterventionService:
    def __init__(self, engine: Engine, store: FileStore):
        self.engine = engine
        self.store = store
        self.repository = InterventionRepository()

    def create(self, task_id: str, intent_id: str, body: CreateIntervention) -> Intervention:
        with transaction(self.engine) as conn:
            task = conn.execute(select(tasks).where(tasks.c.id == task_id)).mappings().one_or_none()
            if task is None:
                raise NotFoundError(f"Task not found: {task_id}")
            intent = self.repository.intent(conn, task_id, intent_id)
            if intent is None or intent["archived"]:
                raise NotFoundError(f"Explore Intent not found: {task_id}/{intent_id}")
            if intent["kind"] != "explore":
                raise ConflictError("只能向 Explore Intent 发送指导消息")
            if intent["status"] not in {"claimed", "running", "concluding"} or not intent["lease_id"]:
                raise ConflictError("该 Explore 当前没有活动 Lease")
            existing = self.repository.get_by_client_id(conn, task_id, intent_id, body.client_message_id)
            if existing is not None:
                return existing
            return self.repository.insert(
                conn,
                task_id=task_id,
                intent_id=intent_id,
                lease_id=intent["lease_id"],
                body=body,
                intervention_id=f"intv_{uuid4().hex}",
                now=utcnow(),
            )

    def claim(self, task_id: str, intent_id: str, lease_id: str, limit: int = 8) -> list[Intervention]:
        with transaction(self.engine) as conn:
            self._require_active_lease(conn, task_id, intent_id, lease_id)
            return self.repository.pending(
                conn, task_id=task_id, intent_id=intent_id, lease_id=lease_id, limit=limit
            )

    def resolve(self, task_id: str, intent_id: str, lease_id: str, intervention_id: str, body: ResolveIntervention) -> Intervention:
        with transaction(self.engine) as conn:
            self._require_active_lease(conn, task_id, intent_id, lease_id)
            item = conn.execute(
                select(tasks.c.id).where(tasks.c.id == task_id)
            ).scalar_one_or_none()
            if item is None:
                raise NotFoundError(f"Task not found: {task_id}")
            result = self.repository.resolve(
                conn, task_id=task_id, intervention_id=intervention_id,
                success=body.success, error=body.error,
            )
            if result is None:
                raise ConflictError("人工消息已处理或不存在")
            return result

    def expire(self, task_id: str, intent_id: str, lease_id: str) -> int:
        with transaction(self.engine) as conn:
            return self.repository.expire_lease(
                conn, task_id=task_id, intent_id=intent_id, lease_id=lease_id
            )

    def conversation(
        self,
        task_id: str,
        intent_id: str,
        after_cursor: str = "",
        limit: int = 200,
        *,
        latest: bool = False,
    ) -> dict[str, Any]:
        with self.engine.connect() as conn:
            task = conn.execute(select(tasks).where(tasks.c.id == task_id)).mappings().one_or_none()
            if task is None:
                raise NotFoundError(f"Task not found: {task_id}")
            intent_rows = conn.execute(
                select(intents).where(intents.c.task_id == task_id, intents.c.archived.is_(False)).order_by(intents.c.created_at, intents.c.id)
            ).mappings().all()
            selected = next((row for row in intent_rows if row["id"] == intent_id), None)
            if selected is None:
                raise NotFoundError(f"Intent not found: {task_id}/{intent_id}")
            if selected["kind"] != "explore":
                raise ConflictError("实时对话只支持 Explore Intent")
            interventions = self.repository.list_for_intent(conn, task_id=task_id, intent_id=intent_id)
            audit_rows = conn.execute(
                select(audit_events)
                .where(audit_events.c.task_id == task_id, audit_events.c.intent_id == intent_id)
                .order_by(audit_events.c.created_at, audit_events.c.id)
                .limit(500)
            ).mappings().all()
        events = self._session_events(task_id, intent_id, selected["lease_id"])
        events.extend(self._intervention_events(interventions))
        events.extend(self._audit_events(audit_rows))
        events.sort(key=lambda item: (item.get("created_at") or "", item.get("cursor") or ""))
        if latest:
            offset = max(0, len(events) - max(1, min(limit, 500)))
        else:
            offset = max(0, int(after_cursor or 0)) if str(after_cursor or "").isdigit() else 0
        page = events[offset : offset + max(1, min(limit, 500))]
        return {
            "task_id": task_id,
            "intent_id": intent_id,
            "intent": self._intent_view(selected),
            "intents": [self._intent_view(row) for row in intent_rows if row["kind"] == "explore"],
            "events": [dict(item, cursor=str(offset + index + 1)) for index, item in enumerate(page)],
            "cursor": str(min(len(events), offset + len(page))),
            "has_more": offset + len(page) < len(events),
            "has_previous": offset > 0,
        }

    def _require_active_lease(self, conn, task_id: str, intent_id: str, lease_id: str):
        intent = self.repository.intent(conn, task_id, intent_id)
        if intent is None:
            raise NotFoundError(f"Explore Intent not found: {task_id}/{intent_id}")
        if intent["kind"] != "explore" or intent["lease_id"] != lease_id or intent["status"] not in {"claimed", "running", "concluding"}:
            raise ConflictError("Explore Lease 已失效")
        return intent

    def _session_events(self, task_id: str, intent_id: str, lease_id: str | None) -> list[dict[str, str]]:
        if not lease_id:
            return []
        root = self.store.root / "runs" / task_id / intent_id
        if not root.is_dir():
            return []
        result: list[dict[str, str]] = []
        sequence = 0
        lease_dirs = [path for path in sorted(root.iterdir()) if path.is_dir()]
        if lease_id:
            lease_dirs.sort(key=lambda path: (path.name != lease_id, path.name))
        for lease_dir in lease_dirs:
            session_root = lease_dir / "sessions"
            for path in sorted(session_root.glob("*.jsonl")):
                try:
                    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                except OSError:
                    continue
                for line in lines:
                    sequence += 1
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    created_at = str(event.get("timestamp") or event.get("created_at") or "")
                    if event.get("type") == "message":
                        message = event.get("message") or {}
                        if not isinstance(message, dict):
                            continue
                        role = str(message.get("role") or "")
                        if role == "assistant":
                            for block in message.get("content") or []:
                                if not isinstance(block, dict):
                                    continue
                                if block.get("type") == "toolCall":
                                    tool = str(block.get("name") or block.get("toolName") or "tool")
                                    arguments = block.get("arguments")
                                    if arguments is None:
                                        arguments = block.get("input")
                                    result.append({"cursor": str(sequence), "kind": "tool_call", "summary": f"调用工具：{tool}", "detail": _tool_call_detail(tool, arguments), "created_at": created_at, "source": "session", "status": "", "tool": tool})
                                elif block.get("type") == "text" and str(block.get("text") or "").strip():
                                    text = _safe_text(str(block["text"]), 1_000)
                                    result.append({"cursor": str(sequence), "kind": "action_summary", "summary": "行动摘要", "detail": text, "created_at": created_at, "source": "session", "status": "", "tool": ""})
                        elif role == "toolResult":
                            tool = str(message.get("toolName") or message.get("name") or "tool")
                            result.append({"cursor": str(sequence), "kind": "tool_result", "summary": f"工具结果：{tool}", "detail": _safe_text(_message_text(message), 800), "created_at": created_at, "source": "session", "status": "", "tool": tool})
                    elif event.get("type") in {"compaction", "branch_summary", "agent_end", "agent_settled", "auto_retry_start", "auto_retry_end"}:
                        kind = str(event.get("type"))
                        result.append({"cursor": str(sequence), "kind": kind, "summary": _event_summary(kind, event), "detail": _safe_text(json.dumps(event, ensure_ascii=False), 500), "created_at": created_at, "source": "session", "status": "", "tool": ""})
        return result

    @staticmethod
    def _audit_events(rows) -> list[dict[str, str]]:
        return [{
            "cursor": f"audit:{row['id']}", "kind": "audit", "summary": _safe_text(row["message"], 500),
            "detail": _safe_text(row["payload_json"], 800), "created_at": row["created_at"],
            "source": "audit", "status": row["level"], "tool": "",
        } for row in rows]

    @staticmethod
    def _intervention_events(items: list[Intervention]) -> list[dict[str, str]]:
        return [{
            "cursor": f"intervention:{item.id}", "kind": "intervention", "summary": "人工指导",
            "detail": _safe_text(item.message, 1_000), "created_at": item.created_at,
            "source": "intervention", "status": item.status, "tool": "", "intervention_id": item.id,
        } for item in items]

    @staticmethod
    def _intent_view(row) -> dict[str, Any]:
        return {
            "id": row["id"], "description": row["description"], "status": row["status"],
            "worker": row["worker"], "lease_id": row["lease_id"], "activity_kind": row["activity_kind"],
            "activity_summary": _safe_text(row["activity_summary"], 500), "activity_tool": row["activity_tool"],
            "created_at": row["created_at"], "claimed_at": row["claimed_at"],
            "activity_updated_at": row["activity_updated_at"], "concluded_at": row["concluded_at"],
        }


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(str(item.get("text")) for item in content if isinstance(item, dict) and item.get("text"))


def _safe_text(value: str, limit: int) -> str:
    value = _JWT.sub("[已脱敏令牌]", value)
    value = _SENSITIVE.sub(lambda match: match.group(1) + "[已脱敏]", value)
    value = _CLI_SENSITIVE.sub(lambda match: match.group(1) + "[已脱敏]", value)
    return value.strip()[:limit]


def _tool_call_detail(tool_name: str, arguments: Any) -> str:
    tool = tool_name.strip() or "tool"
    normalized = tool.lower()
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            arguments = {"input": arguments}
    if not isinstance(arguments, dict):
        arguments = {}

    if normalized in {"bash", "shell", "terminal", "exec", "execute"}:
        command = arguments.get("command") or arguments.get("cmd") or arguments.get("script")
        if command:
            return f"命令：\n$ {_safe_text(str(command), 2_000)}"

    if normalized in {"read", "write", "edit"}:
        path = arguments.get("path") or arguments.get("file_path")
        if path:
            action = {"read": "读取文件", "write": "写入文件", "edit": "编辑文件"}[normalized]
            return f"{action}：{_safe_text(str(path), 1_000)}"

    sanitized = _sanitize_tool_arguments(arguments)
    if not sanitized:
        return f"工具：{tool}（无参数）"
    rendered = json.dumps(sanitized, ensure_ascii=False, indent=2)
    return f"工具：{tool}\n参数：\n{_safe_text(rendered, 2_000)}"


def _sanitize_tool_arguments(value: Any, *, key: str = "", depth: int = 0) -> Any:
    if key and _SENSITIVE_KEY.search(key.strip()):
        return "[已脱敏]"
    if depth >= 6:
        return "[层级过深，已省略]"
    if isinstance(value, dict):
        return {
            str(item_key): _sanitize_tool_arguments(item_value, key=str(item_key), depth=depth + 1)
            for item_key, item_value in list(value.items())[:40]
        }
    if isinstance(value, list):
        return [_sanitize_tool_arguments(item, depth=depth + 1) for item in value[:40]]
    if isinstance(value, str):
        return _safe_text(value, 1_000)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _safe_text(str(value), 1_000)


def _event_summary(kind: str, event: dict[str, Any]) -> str:
    return {
        "compaction": "上下文压缩后继续执行",
        "branch_summary": "分支摘要已生成",
        "agent_end": "本轮模型执行结束",
        "agent_settled": "Explore 会话已收口",
        "auto_retry_start": "模型请求自动重试",
        "auto_retry_end": "模型自动重试结束",
    }.get(kind, kind)
