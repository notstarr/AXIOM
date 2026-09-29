from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from axiom.composition import Container
from axiom.shared.dingtalk import DingTalkConfig, DingTalkNotifier, dingtalk_notifier

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/integrations/dingtalk", tags=["dingtalk"])


def _normalize_task_id(raw_id: str) -> str:
    cleaned = raw_id.strip()
    if cleaned.isdigit():
        return f"t_{cleaned}"
    return cleaned


def _verify_dingtalk_signature(timestamp: str | None, sign: str | None, secret: str | None) -> bool:
    if not secret:
        return True
    if not timestamp or not sign:
        return False
    try:
        # 防重放：限制在 1 小时内
        now_ms = time.time() * 1000
        req_ms = float(timestamp)
        if abs(now_ms - req_ms) > 3600 * 1000:
            return False

        secret_enc = secret.encode("utf-8")
        string_to_sign = f"{timestamp}\n{secret}"
        string_to_sign_enc = string_to_sign.encode("utf-8")
        hmac_code = hmac.new(secret_enc, string_to_sign_enc, digestmod=hashlib.sha256).digest()
        expected_sign = base64.b64encode(hmac_code).decode("utf-8")
        return hmac.compare_digest(sign, expected_sign)
    except Exception:
        return False


def _build_markdown_response(title: str, text: str) -> dict[str, Any]:
    return {
        "msgtype": "markdown",
        "markdown": {
            "title": title,
            "text": text,
        },
    }


def _handle_help() -> dict[str, Any]:
    text = (
        "### 🤖 AXIOM 智能安全渗透控制台 — 指令指南\n\n"
        "**📋【任务监控】**\n"
        "- `/tasks` —— 查看当前所有**正在运行**的任务概览\n"
        "- `/status <任务ID>` —— 查询任务状态/运行时间/事实/意图/漏洞统计（如 `/status 172`）\n\n"
        "**🎮【任务控制】**\n"
        "- `/pause <任务ID>` —— 暂停指定任务\n"
        "- `/resume <任务ID>` —— 继续/恢复指定任务（默认不限时持续运行）\n\n"
        "**🛡️【漏洞查询】**\n"
        "- `/vulns <任务ID> [页码]` —— 漏洞清单，编号从新到旧，每页 10 条（如 `/vulns 172 2`）\n"
        "- `/vuln <任务ID> <漏洞ID>` —— 漏洞详情：危害等级/审核结果/利用前提/危害/描述（如 `/vuln 172 v008`）\n\n"
        "*提示：指令不区分大小写，任务ID支持纯数字简写。*"
    )
    return _build_markdown_response("AXIOM 指令指南", text)


def _handle_tasks(container: Container) -> dict[str, Any]:
    tasks = container.tasks.list()
    running_tasks = [t for t in tasks if t.status == "running"]

    if not running_tasks:
        text = "### 📋 当前没有正在运行的任务\n\n可前往 Web 控制台或通过 API 启动新任务。"
        return _build_markdown_response("任务列表", text)

    lines = ["### 📋 正在运行的任务清单\n"]
    for t in running_tasks:
        duration_min = round((t.run_duration_seconds or 0) / 60, 1)
        origin_input = t.origin.original_input if t.origin else ""
        origin_preview = origin_input.split("\n")[0][:40]
        lines.append(
            f"- **`{t.id}`** | **{t.title or '未命名'}**\n"
            f"  - 阶段: `{t.phase}` | 已运行: `{duration_min}` 分钟\n"
            f"  - 目标: `{origin_preview}`\n"
            f"  - 漏洞数: `{t.vulnerability_count}`"
        )

    lines.append("\n*发送 `/status <ID>` 可查看单个任务详细进度*")
    return _build_markdown_response("运行中任务", "\n".join(lines))


def _task_display_status(task) -> str:
    """状态归一化四态：运行中 / 暂停 / 完成 / 等待。"""
    if task.status == "completed":
        return "✅ 完成"
    if task.status == "failed":
        return "❌ 失败"
    if task.status == "stopped":
        return "⏸️ 暂停"
    if task.phase == "waiting":
        return "🟡 等待"
    return "🟢 运行中"


def _format_run_duration(task) -> str:
    """真实运行时长：优先 run_duration_seconds（暂停/停止后为累计有效时长）。"""
    seconds = task.run_duration_seconds or 0
    if seconds <= 0:
        return "刚启动"
    if seconds < 3600:
        return f"{round(seconds / 60)} 分钟"
    hours = seconds // 3600
    minutes = round((seconds % 3600) / 60)
    return f"{hours} 小时 {minutes} 分钟"


def _handle_status(container: Container, task_id: str) -> dict[str, Any]:
    normalized_id = _normalize_task_id(task_id)
    task = container.tasks.get(normalized_id)
    if not task:
        return _build_markdown_response("错误", f"❌ 找不到任务 `{normalized_id}`，请检查 ID 是否正确。")

    origin_input = task.origin.original_input if task.origin else "未知"
    origin_lines = [line.strip() for line in origin_input.split("\n") if line.strip()]
    origin_display = "、".join(f"`{line}`" for line in origin_lines[:3])
    if len(origin_lines) > 3:
        origin_display += f" 等 {len(origin_lines)} 个"

    lines = [
        f"### 任务 `{task.id}` — {task.title or '未命名'}",
        f"- **状态**: {_task_display_status(task)}（阶段: `{task.phase}`）",
        f"- **运行时间**: {_format_run_duration(task)}",
        f"- **事实**: `{task.fact_count}` | **意图**: `{task.intent_count}` | **运行中意图**: `{task.working_intent_count}` | **漏洞**: `{task.vulnerability_count}`",
        f"- **目标**: {task.goal or '未设置'}",
        f"- **起点**: {origin_display}",
    ]
    if task.last_pause_reason:
        reason_label = "到达运行时限" if task.last_pause_reason == "run_deadline_reached" else "人工暂停"
        lines.append(f"- **暂停原因**: {reason_label}")

    return _build_markdown_response(f"任务 {task.id} 状态", "\n".join(lines))


def _handle_pause(container: Container, task_id: str) -> dict[str, Any]:
    normalized_id = _normalize_task_id(task_id)
    try:
        task = container.tasks.pause_manual(normalized_id)
        return _build_markdown_response(
            "任务已暂停",
            f"⏸️ **任务 `{task.id}` 已成功暂停**\n\n当前阶段: `{task.phase}`。如需恢复，请发送 `/resume {task.id}`。"
        )
    except Exception as e:
        return _build_markdown_response("暂停失败", f"❌ 暂停任务 `{normalized_id}` 失败: {str(e)}")


def _handle_resume(container: Container, task_id: str) -> dict[str, Any]:
    normalized_id = _normalize_task_id(task_id)
    try:
        # 继续任务默认不限时持续运行 (command=None)
        task = container.tasks.resume(normalized_id, None)
        return _build_markdown_response(
            "任务已继续",
            f"▶️ **任务 `{task.id}` 已恢复执行**\n\n运行模式: **不限时持续运行** | 当前阶段: `{task.phase}`。"
        )
    except Exception as e:
        return _build_markdown_response("恢复失败", f"❌ 恢复任务 `{normalized_id}` 失败: {str(e)}")


def _severity_rank(severity: str) -> int:
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    return order.get((severity or "").lower(), 5)


def _severity_label(severity: str) -> str:
    mapping = {
        "critical": "🔴 严重",
        "high": "🟠 高危",
        "medium": "🟡 中危",
        "low": "🔵 低危",
        "info": "⚪ 提示",
    }
    return mapping.get((severity or "").lower(), f"⚠️ {severity}")


def _vuln_number(v) -> int:
    """漏洞编号 vNNN 的数字部分；解析失败排最后。"""
    digits = "".join(ch for ch in (v.id or "") if ch.isdigit())
    return int(digits) if digits else 10**9


def _handle_vulns(container: Container, task_id: str, page: int = 1) -> dict[str, Any]:
    normalized_id = _normalize_task_id(task_id)
    try:
        vulns = container.vulnerabilities.list(normalized_id)
    except Exception as e:
        return _build_markdown_response("查询失败", f"❌ 查询任务 `{normalized_id}` 漏洞失败: {str(e)}")

    if not vulns:
        return _build_markdown_response(f"任务 {normalized_id} 漏洞清单", f"🛡️ 任务 `{normalized_id}` 当前暂未发现漏洞。")

    # 编号数字从大到小
    ordered = sorted(vulns, key=_vuln_number, reverse=True)
    page_size = 10
    total_pages = (len(ordered) + page_size - 1) // page_size
    page = max(1, min(page, total_pages))
    chunk = ordered[(page - 1) * page_size : page * page_size]

    lines = [f"### 🛡️ 任务 `{normalized_id}` 漏洞清单（共 {len(ordered)} 个，第 {page}/{total_pages} 页）\n"]
    for v in chunk:
        lines.append(f"- **`{v.id}`** {_severity_label(v.severity)} | {v.status} | {v.title}")
    if total_pages > 1:
        lines.append(f"\n*翻页：`/vulns {normalized_id} <页码>`（如 `/vulns {normalized_id} 2`）*")
    lines.append(f"*详情：`/vuln {normalized_id} <漏洞ID>`*")
    return _build_markdown_response(f"任务 {normalized_id} 漏洞清单 {page}/{total_pages}", "\n".join(lines))


def _handle_vuln(container: Container, task_id: str, vuln_id: str) -> dict[str, Any]:
    from axiom.shared.dingtalk import DingTalkNotifier

    normalized_task_id = _normalize_task_id(task_id)
    cleaned_vuln_id = vuln_id.strip()
    try:
        # 漏洞 ID 是每任务内独立编号（v001 从每个任务重新开始），必须限定任务查询，
        # 否则会撞到其他任务里的同名 ID。
        vulns = container.vulnerabilities.list(normalized_task_id)
    except Exception as e:
        return _build_markdown_response(
            "查询失败",
            f"❌ 查询任务 `{normalized_task_id}` 漏洞失败: {str(e)}",
        )

    candidates = {cleaned_vuln_id, f"v{cleaned_vuln_id}"}
    if cleaned_vuln_id.isdigit():
        candidates.add(f"v{int(cleaned_vuln_id):03d}")  # 64 → v064
    target = next((v for v in vulns if v.id in candidates), None)
    if not target:
        return _build_markdown_response(
            "错误",
            f"❌ 任务 `{normalized_task_id}` 下找不到漏洞 `{cleaned_vuln_id}`。"
            f"\n可用 `/vulns {normalized_task_id}` 查看该任务全部漏洞清单。",
        )

    fmt = DingTalkNotifier.__new__(DingTalkNotifier)
    lines = [
        f"### 🔍 `{target.id}` — {target.title}",
        f"- **危害等级**: {_severity_label(target.severity)}",
        f"- **审核结果**: **{target.status}**",
    ]
    if target.affected_target:
        lines.append(f"- **受影响目标**: `{target.affected_target}`")
    prereq_text = fmt._format_description(target.prerequisites or "", limit=300)
    if prereq_text:
        lines.append(f"\n**利用前提**:\n\n{prereq_text}")
    impact_text = fmt._format_description(target.impact or "", limit=300)
    if impact_text:
        lines.append(f"\n**危害（影响）**:\n\n{impact_text}")
    desc_text = fmt._format_description(target.description or "", limit=300)
    if desc_text:
        lines.append(f"\n**描述**:\n\n{desc_text}")
    if target.status_reason:
        lines.append(f"\n**裁决理由**: {fmt._format_description(target.status_reason, limit=300)}")

    return _build_markdown_response(f"漏洞 {target.id}", "\n".join(lines))


def route_dingtalk_command(container: Container, text_content: str) -> dict[str, Any]:
    """解析钉钉群消息并按指令路由，返回 markdown 响应体。

    Webhook 回调与 Stream 模式共用同一路由逻辑。
    """
    text = (text_content or "").strip()
    if not text:
        return _handle_help()

    parts = text.split()
    cmd = parts[0].lower()
    args = parts[1:]

    if cmd in ("/help", "help", "帮助", "/start"):
        return _handle_help()
    if cmd in ("/tasks", "tasks", "/task", "任务"):
        return _handle_tasks(container)
    if cmd in ("/status", "status", "/stat", "状态"):
        if not args:
            return _build_markdown_response("提示", "⚠️ 请指定任务 ID，例如：`/status t_172` 或 `/status 172`")
        return _handle_status(container, args[0])
    if cmd in ("/pause", "pause", "暂停"):
        if not args:
            return _build_markdown_response("提示", "⚠️ 请指定任务 ID，例如：`/pause 172`")
        return _handle_pause(container, args[0])
    if cmd in ("/resume", "resume", "继续", "恢复"):
        if not args:
            return _build_markdown_response("提示", "⚠️ 请指定任务 ID，例如：`/resume 172`")
        return _handle_resume(container, args[0])
    if cmd in ("/vulns", "vulns", "/vulnerabilities", "漏洞列表"):
        if not args:
            return _build_markdown_response("提示", "⚠️ 请指定任务 ID，例如：`/vulns 172`（可带页码 `/vulns 172 2`）")
        page = 1
        if len(args) > 1 and str(args[1]).isdigit():
            page = int(args[1])
        return _handle_vulns(container, args[0], page=page)
    if cmd in ("/vuln", "vuln", "漏洞"):
        if len(args) < 2:
            return _build_markdown_response(
                "提示",
                "⚠️ 漏洞 ID 是每个任务内独立编号，请同时指定任务 ID：`/vuln 172 v008`\n"
                "（可用 `/vulns 172` 查看该任务漏洞清单）",
            )
        return _handle_vuln(container, args[0], args[1])
    return _handle_help()


@router.post("/webhook")
async def dingtalk_incoming_webhook(
    request: Request,
    timestamp: str | None = Header(None),
    sign: str | None = Header(None),
) -> JSONResponse:
    """接收钉钉自定义机器人/群快捷指令回调"""
    cfg = dingtalk_notifier.config
    if cfg and cfg.secret:
        if not _verify_dingtalk_signature(timestamp, sign, cfg.secret):
            raise HTTPException(status_code=401, detail="Invalid DingTalk signature")

    body = await request.json()
    container: Container = request.app.state.container

    # 提取用户发送的文本内容
    text_content = ""
    if "text" in body and "content" in body["text"]:
        text_content = body["text"]["content"].strip()
    elif "content" in body:
        text_content = str(body["content"]).strip()

    resp = route_dingtalk_command(container, text_content)
    return JSONResponse(content=resp)


from pydantic import BaseModel

class DingTalkTestPayload(BaseModel):
    webhook_url: str | None = None
    secret: str | None = None


@router.post("/test")
async def dingtalk_test_notification(payload: DingTalkTestPayload | None = None) -> dict[str, Any]:
    """一键测试钉钉 Webhook 连通性（支持直接传入输入框中的 URL 与 Secret 进行即时测试）"""
    url = (payload.webhook_url.strip() if payload and payload.webhook_url else None)
    secret = (payload.secret.strip() if payload and payload.secret else None)

    if url:
        notifier = DingTalkNotifier(DingTalkConfig(webhook_url=url, secret=secret, enabled=True))
    else:
        notifier = dingtalk_notifier

    if not notifier.is_enabled:
        raise HTTPException(status_code=400, detail="请先输入有效的 Webhook URL（以 https:// 开头）。")

    ok = notifier.send_markdown(
        "AXIOM 连通性测试",
        "### 🔔 AXIOM 钉钉通知测试成功\n\n"
        "当前系统已成功连接至钉钉机器人！\n"
        "- 漏洞发现时将实时推送初报卡片\n"
        "- 漏洞审核完成后将实时推送裁决理由\n\n"
        "*测试时间: " + time.strftime("%Y-%m-%d %H:%M:%S") + "*"
    )
    if not ok:
        raise HTTPException(status_code=400, detail="钉钉通知发送失败，请检查 Webhook URL 和 Secret 是否正确，或查看服务端网络能否访问钉钉。")
    return {"status": "ok", "message": "测试通知已成功发送至钉钉群！"}
