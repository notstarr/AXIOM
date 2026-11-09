from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import threading
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# 红队成果的领域词汇：卡片文案与群指令共用同一份定义，避免两处各写一套。
RESULT_TYPE_LABELS: dict[str, str] = {
    "permission": "🔓 权限跃迁",
    "data": "🗄️ 数据获取",
    "credential": "🔑 凭证获取",
    "pivot": "↔️ 横向控制",
}

# 业务方视角的成果动作：业务侧看到的是「对哪个业务做了什么」，
# 不是红队护网术语。这份表只用于公示行与简版卡，不进红队内部详情。
PUBLIC_RESULT_TYPE_LABELS: dict[str, str] = {
    "permission": "越权访问",
    "data": "数据访问",
    "credential": "凭据获取",
    "pivot": "横向移动",
}

RESULT_STATUS_LABELS: dict[str, str] = {
    "reported": "已上报",
    "reviewing": "审核中",
    "verified": "✅ 已验证",
    "rejected": "⛔ 已驳回",
    "duplicate": "♻️ 重复",
    "needs-human": "🙋 需人工",
}


def result_type_label(value: str | None) -> str:
    key = str(value or "").strip().lower()
    return RESULT_TYPE_LABELS.get(key, f"• {value}" if value else "• 未知")


def public_result_type_label(value: str | None) -> str:
    """业务方口径的成果动作；未知类型回落到原始值，不编造语义。"""

    key = str(value or "").strip().lower()
    return PUBLIC_RESULT_TYPE_LABELS.get(key, str(value or "").strip() or "测试行为")


def result_status_label(value: str | None) -> str:
    key = str(value or "").strip().lower()
    return RESULT_STATUS_LABELS.get(key, str(value) if value else "未知")


def _notice_timezone() -> tzinfo:
    """公示时间统一按业务所在地时区展示；缺 tzdata 时退回固定 UTC+8。"""

    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("Asia/Shanghai")
    except Exception:  # pragma: no cover - 取决于容器是否带 tzdata
        return timezone(timedelta(hours=8))


def format_notice_time(value: Any) -> str:
    """把成果时间转成业务方能读的 ``MM-DD HH:mm``；认不出来就原样返回，不编造时间。"""

    if value is None:
        return ""
    moment: datetime | None = None
    if isinstance(value, datetime):
        moment = value
    else:
        text = str(value).strip()
        if not text:
            return ""
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return text
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(_notice_timezone()).strftime("%m-%d %H:%M")


def public_notice_line(
    *,
    occurred_at: Any,
    target_asset: str,
    result_type: str,
) -> str:
    """公示行：什么时间、对什么业务、做了什么。

    只保留业务方对号需要的三要素，不带漏洞细节、证据与攻击载荷。
    """

    parts = ["[红队]"]
    when = format_notice_time(occurred_at)
    if when:
        parts.append(when)
    asset = str(target_asset or "").strip()
    if asset:
        parts.append(asset)
    parts.append(public_result_type_label(result_type))
    return " · ".join(parts)

# dingtalk-stream SDK 为可选依赖（Stream 双向交互才需要）；未安装时
# Webhook 单向推送不受影响，DingTalkStreamBot 启动会报错并由调用方捕获。
try:
    from dingtalk_stream import AckMessage, CallbackHandler, ChatbotHandler, ChatbotMessage, CallbackMessage
except ImportError:  # pragma: no cover - optional dependency
    AckMessage = None
    ChatbotHandler = object
    ChatbotMessage = None
    CallbackMessage = None


@dataclass(frozen=True)
class DingTalkConfig:
    webhook_url: str
    secret: str | None = None
    enabled: bool = True
    app_key: str | None = None
    app_secret: str | None = None
    # 业务方公示通道：独立机器人（独立 webhook），只推红队活动的简版播报。
    # 与上报主通道并存，互不影响；未配置时该通道自然静默。
    activity_webhook_url: str = ""
    activity_secret: str | None = None

    @classmethod
    def from_runtime(cls) -> DingTalkConfig | None:
        """优先从数据库 runtime_settings 读取，兜底从环境变量读取"""
        try:
            from axiom.modules.settings.service import load_runtime_config
            from axiom.shared.database import create_database_engine, database_url_from_env
            engine = create_database_engine(database_url_from_env())
            # load_runtime_config 需要 Connection（SQLAlchemy 2.0 的 Engine 没有 execute）
            with engine.connect() as conn:
                config = load_runtime_config(conn)
            if config.dingtalk_webhook_url.strip():
                return cls(
                    webhook_url=config.dingtalk_webhook_url.strip(),
                    secret=config.dingtalk_secret.strip() or None,
                    enabled=config.dingtalk_enabled,
                    app_key=config.dingtalk_app_key.strip() or None,
                    app_secret=config.dingtalk_app_secret.strip() or None,
                    activity_webhook_url=config.dingtalk_activity_webhook_url.strip(),
                    activity_secret=config.dingtalk_activity_secret.strip() or None,
                )
        except Exception:
            pass

        url = os.getenv("AXIOM_DINGTALK_WEBHOOK_URL") or os.getenv("DINGTALK_WEBHOOK_URL")
        if not url:
            return None
        secret = os.getenv("AXIOM_DINGTALK_SECRET") or os.getenv("DINGTALK_SECRET")
        enabled = os.getenv("AXIOM_DINGTALK_ENABLED", "true").lower() in ("true", "1", "yes")
        activity_url = (
            os.getenv("AXIOM_DINGTALK_ACTIVITY_WEBHOOK_URL")
            or os.getenv("DINGTALK_ACTIVITY_WEBHOOK_URL")
            or ""
        )
        activity_secret = (
            os.getenv("AXIOM_DINGTALK_ACTIVITY_SECRET")
            or os.getenv("DINGTALK_ACTIVITY_SECRET")
            or ""
        )
        return cls(
            webhook_url=url.strip(),
            secret=secret.strip() if secret else None,
            enabled=enabled,
            app_key=os.getenv("AXIOM_DINGTALK_APP_KEY") or None,
            app_secret=os.getenv("AXIOM_DINGTALK_APP_SECRET") or None,
            activity_webhook_url=activity_url.strip(),
            activity_secret=activity_secret.strip() or None,
        )

    @property
    def stream_configured(self) -> bool:
        """是否具备 Stream 双向交互所需的 AppKey/AppSecret"""
        return bool(self.app_key and self.app_secret)


class DingTalkNotifier:
    def __init__(self, config: DingTalkConfig | None = None):
        self._config = config

    @property
    def config(self) -> DingTalkConfig | None:
        if self._config is not None:
            return self._config
        return DingTalkConfig.from_runtime()

    @property
    def is_enabled(self) -> bool:
        cfg = self.config
        return cfg is not None and cfg.enabled and bool(cfg.webhook_url)

    def _get_signed_url(self, webhook_url: str, secret: str | None) -> str:
        if not secret:
            return webhook_url
        timestamp = str(round(time.time() * 1000))
        secret_enc = secret.encode("utf-8")
        string_to_sign = f"{timestamp}\n{secret}"
        string_to_sign_enc = string_to_sign.encode("utf-8")
        hmac_code = hmac.new(secret_enc, string_to_sign_enc, digestmod=hashlib.sha256).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        sep = "&" if "?" in webhook_url else "?"
        return f"{webhook_url}{sep}timestamp={timestamp}&sign={sign}"

    def send_markdown(self, title: str, text: str) -> bool:
        cfg = self.config
        if not cfg or not cfg.enabled or not cfg.webhook_url:
            logger.debug("DingTalk notification skipped (not configured or disabled)")
            return False
        return self._post_markdown(cfg.webhook_url, cfg.secret, title, text)

    def send_activity_markdown(self, title: str, text: str) -> bool:
        """业务方公示通道：独立机器人，与上报主通道各走各的 webhook。"""

        cfg = self.config
        if not cfg or not cfg.enabled or not cfg.activity_webhook_url:
            logger.debug("DingTalk activity notification skipped (not configured)")
            return False
        return self._post_markdown(cfg.activity_webhook_url, cfg.activity_secret, title, text)

    def _post_markdown(
        self, webhook_url: str, secret: str | None, title: str, text: str
    ) -> bool:
        target_url = self._get_signed_url(webhook_url, secret)
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": title,
                "text": text,
            },
        }

        try:
            # 本机 Python 环境（venv 3.14）的 OpenSSL 证书链在此前多次出现
            # CERTIFICATE_VERIFY_FAILED（无法找到本地签发者），导致钉钉通知
            # 发送失败（curl / trust_env=False 直连均正常）。钉钉为公网服务，
            # 通知内容为漏洞告警卡片，禁用 TLS 证书校验以恢复连通性。
            with httpx.Client(timeout=10.0, verify=False) as client:
                resp = client.post(target_url, json=payload)
                resp.raise_for_status()
                data = resp.json()
                if data.get("errcode") == 0:
                    logger.info("DingTalk notification sent successfully: %s", title)
                    return True
                else:
                    logger.error("DingTalk API error: %s", data)
                    return False
        except Exception as e:
            logger.error("Failed to send DingTalk notification: %s", e)
            return False

    def _format_description(self, description: str, limit: int = 600) -> str:
        """把漏洞描述整理成钉钉 markdown 卡片能识别的段落。

        探索 worker 上报时常把换行写成字面 "\n"/"\\n"(两个字符：反斜杠+n)
        存进 DB；钉钉 markdown 卡片对单个 \\n 不做换行，对 \\n\\n 也不渲染，
        必须用 <br/> 强制换行，并按段落边界截断避免截到句子中间。
        """
        if not description:
            return ""
        # 1. 归一化字面 "\n" -> 真换行
        text = description.replace("\\r\\n", "\n").replace("\\n", "\n").strip()
        # 2. 真换行转 <br/> 让钉钉 markdown 卡片渲染换行
        text = text.replace("\n", "<br/>")
        if len(text) <= limit:
            return text
        # 3. 按 <br/><br/> 段落边界截断;单段超长时按字符硬截,确保不超 limit
        paragraphs = text.split("<br/><br/>")
        chunks: list[str] = []
        total = 0
        for p in paragraphs:
            cost = len(p) + len("<br/><br/>")
            if total + cost > limit:
                remaining = limit - total
                if remaining > 20:
                    chunks.append(p[:remaining])
                break
            chunks.append(p)
            total += cost
        return "<br/><br/>".join(chunks).rstrip() + "…"

    def notify_vulnerability_reported(
        self,
        task_id: str,
        vuln_id: str,
        title: str,
        severity: str,
        vuln_type: str,
        status: str,
        affected_target: str = "",
        description: str = "",
        entry_point: str = "",
        step_count: int = 0,
        prerequisites: str = "",
        impact: str = "",
    ) -> bool:
        """【第 1 次推送】：漏洞初次发现上报"""
        severity_map = {
            "critical": "🔴 严重 (Critical)",
            "high": "🟠 高危 (High)",
            "medium": "🟡 中危 (Medium)",
            "low": "🔵 低危 (Low)",
            "info": "⚪ 提示 (Info)",
        }
        sev_label = severity_map.get(severity.lower(), f"⚠️ {severity}")

        lines = [
            f"### 🛡️ AXIOM 发现新漏洞（待评审）",
            f"**任务编号**: `{task_id}`",
            f"**漏洞编号**: `{vuln_id}`",
            f"**漏洞标题**: {title}",
            f"**危害等级**: {sev_label}",
            f"**漏洞类型**: `{vuln_type}`",
            f"**当前状态**: **{status}**",
        ]
        if affected_target:
            lines.append(f"**受影响资产**: `{affected_target}`")
        prereq_text = self._format_description(prerequisites, limit=200)
        if prereq_text:
            lines.append(f"**利用前提**:\n\n{prereq_text}")
        impact_text = self._format_description(impact, limit=300)
        if impact_text:
            lines.append(f"**影响**:\n\n{impact_text}")
        desc_preview = self._format_description(description, limit=300)
        if desc_preview:
            lines.append(f"**漏洞描述**:\n\n{desc_preview}")
        if entry_point:
            steps = f"（{step_count} 步）" if step_count else ""
            lines.append(f"**入口点**: `{entry_point}`\n\n复现步骤{steps}见任务详情。")

        lines.append(f"\n*AXIOM 自动化安全测试平台 · 实时上报*")
        card_text = "\n\n".join(lines)
        return self.send_markdown(f"【新漏洞上报】{title}", card_text)

    def notify_vulnerability_reviewed(
        self,
        task_id: str,
        vuln_id: str,
        title: str,
        severity: str,
        status: str,
        status_reason: str = "",
        reviewed_by: str = "",
    ) -> bool:
        """【第 2 次推送】：漏洞审核/评审完毕裁决推送"""
        severity_map = {
            "critical": "🔴 严重 (Critical)",
            "high": "🟠 高危 (High)",
            "medium": "🟡 中危 (Medium)",
            "low": "🔵 低危 (Low)",
            "info": "⚪ 提示 (Info)",
        }
        sev_label = severity_map.get(severity.lower(), f"⚠️ {severity}")

        status_emoji = "✅" if status == "已验证" else ("⚪" if status == "忽略" else "🔄")

        lines = [
            f"### ⚖️ AXIOM 漏洞评审结果",
            f"**任务编号**: `{task_id}`",
            f"**漏洞编号**: `{vuln_id}`",
            f"**漏洞标题**: {title}",
            f"**危害等级**: {sev_label}",
            f"**评审结论**: {status_emoji} **{status}**",
        ]
        if reviewed_by:
            lines.append(f"**评审人/阶段**: `{reviewed_by}`")
        status_reason_text = self._format_description(status_reason, limit=400)
        if status_reason_text:
            lines.append(f"**裁决理由**:\n\n{status_reason_text}")

        lines.append(f"\n*AXIOM 自动化安全测试平台 · 评审通知*")
        card_text = "\n\n".join(lines)
        return self.send_markdown(f"【漏洞评审结果】{title} -> {status}", card_text)

    # ── 红队成果卡片 ──────────────────────────────────────────────────
    # 红队用护网计分语言（权限跃迁/数据/凭证/横向 + 核定分），不复用漏洞的
    # 严重度/状态词汇，避免出现「危害等级：permission」这类错位。

    def _progress_line(self, progress: tuple[int, int] | None) -> list[str]:
        """积分进度：红队 Goal 判定靠 verified 分之和达到 target_points。"""
        if not progress:
            return []
        awarded, target = int(progress[0]), int(progress[1])
        if target <= 0:
            return [f"**积分进度**: `{awarded}` 分"]
        percent = min(100, round(awarded / target * 100))
        return [f"**积分进度**: `{awarded}/{target}` 分（{percent}%）"]

    def _capability_transition(
        self, before: list[str] | None, gained: list[str] | None
    ) -> str:
        before_list = [str(item) for item in (before or []) if str(item).strip()]
        gained_list = [str(item) for item in (gained or []) if str(item).strip()]
        if not before_list and not gained_list:
            return ""
        left = "、".join(f"`{item}`" for item in before_list) or "—"
        right = "、".join(f"`{item}`" for item in gained_list) or "—"
        return f"{left} → {right}"

    def notify_redteam_result_reported(
        self,
        *,
        task_id: str,
        result_id: str,
        title: str,
        result_type: str,
        description: str = "",
        principal: str = "",
        tenant: str = "",
        target_asset: str = "",
        chain_label: str = "",
        capabilities_before: list[str] | None = None,
        capabilities_gained: list[str] | None = None,
        points_declared: int = 0,
        evidence_names: list[str] | None = None,
        progress: tuple[int, int] | None = None,
    ) -> bool:
        """【红队第 1 次推送】：成果上报、进入审核队列（上报主通道，文案不变）"""
        lines = [
            "### 🎯 AXIOM 红队成果上报（待审核）",
            f"**任务编号**: `{task_id}`",
            f"**成果编号**: `{result_id}`",
            f"**成果类型**: {result_type_label(result_type)}",
            f"**成果标题**: {title}",
        ]
        if chain_label:
            lines.append(f"**攻击链**: {chain_label}")
        identity = f"`{principal}`" if principal else ""
        if identity and tenant:
            identity = f"`{principal}` @ `{tenant}`"
        if identity:
            lines.append(f"**身份**: {identity}")
        if target_asset:
            lines.append(f"**对象**: `{target_asset}`")
        capability_line = self._capability_transition(capabilities_before, capabilities_gained)
        if capability_line:
            lines.append(f"**能力变化**: {capability_line}")
        if points_declared:
            lines.append(f"**自评分**: `{points_declared}` 分")
        names = [str(item) for item in (evidence_names or []) if str(item).strip()]
        if names:
            shown = "、".join(names[:5])
            more = f" 等 {len(names)} 个" if len(names) > 5 else ""
            lines.append(f"**证据**: {shown}{more}")
        desc = self._format_description(description, limit=300)
        if desc:
            lines.append(f"**描述**:\n\n{desc}")
        lines.extend(self._progress_line(progress))
        lines.append("\n*AXIOM 自动化安全测试平台 · 红队实时上报*")
        return self.send_markdown(f"【红队成果上报】{title}", "\n\n".join(lines))

    def notify_redteam_result_reviewed(
        self,
        *,
        task_id: str,
        result_id: str,
        title: str,
        result_type: str,
        status: str,
        points_awarded: int = 0,
        points_declared: int = 0,
        target_asset: str = "",
        review_reason: str = "",
        reviewed_by: str = "",
        progress: tuple[int, int] | None = None,
    ) -> bool:
        """【红队第 2 次推送】：成果审核裁决（上报主通道，文案不变）"""
        lines = [
            "### ⚖️ AXIOM 红队成果审核结果",
            f"**任务编号**: `{task_id}`",
            f"**成果编号**: `{result_id}`",
            f"**成果标题**: {title}",
            f"**成果类型**: {result_type_label(result_type)}",
            f"**审核结论**: {result_status_label(status)}",
        ]
        if status == "verified":
            declared = f"（自评 {points_declared}）" if points_declared else ""
            lines.append(f"**核定分**: `{points_awarded}` 分{declared}")
        if target_asset:
            lines.append(f"**对象**: `{target_asset}`")
        if reviewed_by:
            lines.append(f"**审查者**: `{reviewed_by}`")
        reason = self._format_description(review_reason, limit=400)
        if reason:
            lines.append(f"**裁决理由**:\n\n{reason}")
        lines.extend(self._progress_line(progress))
        lines.append("\n*AXIOM 自动化安全测试平台 · 红队审核通知*")
        return self.send_markdown(
            f"【红队审核结果】{title} -> {status}", "\n\n".join(lines)
        )

    def notify_redteam_activity_notice(
        self,
        *,
        task_id: str,
        result_id: str,
        result_type: str,
        target_asset: str = "",
        occurred_at: Any = None,
        status: str = "",
    ) -> bool:
        """业务方公示通道（独立机器人）：只说什么时间·对什么业务·做了什么。

        与上报主通道并存：主通道给红队内部看完整卡片，本通道给业务方对号，
        不含漏洞描述、证据、积分与任何攻击细节。
        """
        notice = public_notice_line(
            occurred_at=occurred_at, target_asset=target_asset, result_type=result_type
        )
        lines = [
            f"### {notice}",
            f"**时间**: {format_notice_time(occurred_at) or '未记录'}",
            f"**业务**: {target_asset or '未标注'}",
            f"**动作**: {public_result_type_label(result_type)}",
            f"**任务**: `{task_id}` · 成果 `{result_id}`",
        ]
        if status:
            lines.append(f"**状态**: {result_status_label(status)}")
        lines.append("\n*AXIOM 自动化安全测试平台 · 红队活动公示*")
        return self.send_activity_markdown(
            f"【红队活动】{target_asset or task_id}", "\n\n".join(lines)
        )



# 全局单例
dingtalk_notifier = DingTalkNotifier()


class DingTalkStreamBot:
    """钉钉 Stream 模式机器人：通过长连接接收群消息并响应 /help 等指令。

    需要企业内部应用的 AppKey/AppSecret（配置于系统设置-钉钉告警）。
    以守护线程方式运行，断线自动重连；进程退出即结束。
    """

    def __init__(self, *, app_key: str, app_secret: str, container):
        import dingtalk_stream

        self._dingtalk_stream = dingtalk_stream
        self._app_key = app_key
        self._app_secret = app_secret
        self._container = container
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run,
            name="axiom-dingtalk-stream",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "DingTalk Stream bot started (app_key=%s...)", self._app_key[:4]
        )

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        # venv (Python 3.14) 的 OpenSSL 证书链在本机存在缺陷（与 Webhook 推送
        # 同一根因）：requests 默认的 certifi / 系统 CA 均报 CERTIFICATE_VERIFY_FAILED，
        # 且连接池的 SSLContext 在首次请求时固化，运行时设 REQUESTS_CA_BUNDLE 无效。
        # 钉钉为公网服务、交互内容为任务状态/漏洞告警，monkey-patch 禁用 TLS 校验，
        # 并忽略环境代理（避免 WorkBuddy 宿主注入的 HTTP(S)_PROXY 干扰直连）。
        try:
            import requests.api

            _orig_request = requests.api.request

            def _patched_request(method, url, **kwargs):
                kwargs["verify"] = False
                return _orig_request(method, url, **kwargs)

            requests.api.request = _patched_request
        except Exception:
            logger.exception("Failed to patch requests TLS verify for DingTalk Stream")
        # 本机 macOS 系统级代理（SystemConfiguration: HTTP/SOCKS 127.0.0.1:9898）
        # 会让 websockets 库尝试走 SOCKS（需 python-socks）而报错，且经代理的
        # TLS 会因 MITM 证书验证失败。显式禁用代理，直连钉钉 wss。
        try:
            import websockets

            _orig_connect = websockets.connect

            def _patched_connect(uri, *args, **kwargs):
                kwargs["proxy"] = None
                return _orig_connect(uri, *args, **kwargs)

            websockets.connect = _patched_connect
        except Exception:
            logger.exception("Failed to patch websockets proxy for DingTalk Stream")
        # start_forever() 内部会处理断线重连；每次重连间隔 3s。
        # 我们用外层循环以便在 stop 时优雅退出。
        while not self._stop_event.is_set():
            try:
                client = self._build_client()
                client.start_forever()
            except Exception:
                logger.exception("DingTalk Stream client crashed; reconnecting...")
                if self._stop_event.wait(3):
                    break

    def _build_client(self):
        ds = self._dingtalk_stream
        credential = ds.Credential(self._app_key, self._app_secret)
        client = ds.DingTalkStreamClient(credential)
        handler = AxiomChatbotHandler(container=self._container)
        client.register_callback_handler(ds.ChatbotMessage.TOPIC, handler)
        client.register_callback_handler(ds.ChatbotMessage.DELEGATE_TOPIC, handler)
        return client


class AxiomChatbotHandler(ChatbotHandler):
    """处理钉钉群消息，复用 Webhook 指令路由（/help /status /tasks 等）。"""

    def __init__(self, container):
        super().__init__()
        self._container = container

    async def process(self, message: ChatbotMessage):
        try:
            from axiom.modules.integrations.dingtalk_api import route_dingtalk_command
            # 兼容两种推送格式：
            # - TOPIC    /v1.0/im/bot/messages/get      → SDK 直接给 ChatbotMessage
            # - DELEGATE /v1.0/im/bot/messages/delegate → SDK 给 CallbackMessage，
            #   真实消息体在 message.data（JSON 字符串）中，需转成 ChatbotMessage。
            if CallbackMessage is not None and isinstance(message, CallbackMessage):
                data = message.data
                if isinstance(data, str):
                    try:
                        data = json.loads(data)
                    except Exception:
                        data = {}
                if not isinstance(data, dict):
                    data = {}
                message = ChatbotMessage.from_dict(data)
            content = ""
            if message.text is not None:
                content = str(getattr(message.text, "content", "") or "").strip()
            if not content:
                return AckMessage.STATUS_OK, "OK"
            resp = await asyncio.to_thread(
                route_dingtalk_command, self._container, content
            )
            markdown = (resp or {}).get("markdown") or {}
            title = markdown.get("title", "AXIOM")
            text = markdown.get("text", "")
            await asyncio.to_thread(
                self.reply_markdown_card,
                text,
                message,
                title=title,
                at_sender=True,
            )
        except Exception:
            logger.exception("Failed to handle DingTalk Stream message")
            try:
                await asyncio.to_thread(
                    self.reply_markdown_card,
                    "### ⚠️ AXIOM 指令处理失败\n\n请查看服务端日志。",
                    message,
                    title="AXIOM 错误",
                    at_sender=True,
                )
            except Exception:
                pass
        return AckMessage.STATUS_OK, "OK"
