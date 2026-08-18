from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import os
import threading
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# dingtalk-stream SDK 为可选依赖（Stream 双向交互才需要）；未安装时
# Webhook 单向推送不受影响，DingTalkStreamBot 启动会报错并由调用方捕获。
try:
    from dingtalk_stream import AckMessage, ChatbotHandler, ChatbotMessage
except ImportError:  # pragma: no cover - optional dependency
    AckMessage = None
    ChatbotHandler = object
    ChatbotMessage = None


@dataclass(frozen=True)
class DingTalkConfig:
    webhook_url: str
    secret: str | None = None
    enabled: bool = True
    app_key: str | None = None
    app_secret: str | None = None

    @classmethod
    def from_runtime(cls) -> DingTalkConfig | None:
        """优先从数据库 runtime_settings 读取，兜底从环境变量读取"""
        try:
            from axiom.modules.settings.service import load_runtime_config
            from axiom.shared.database import create_engine_from_env
            engine = create_engine_from_env()
            config = load_runtime_config(engine)
            if config.dingtalk_webhook_url.strip():
                return cls(
                    webhook_url=config.dingtalk_webhook_url.strip(),
                    secret=config.dingtalk_secret.strip() or None,
                    enabled=config.dingtalk_enabled,
                    app_key=config.dingtalk_app_key.strip() or None,
                    app_secret=config.dingtalk_app_secret.strip() or None,
                )
        except Exception:
            pass

        url = os.getenv("AXIOM_DINGTALK_WEBHOOK_URL") or os.getenv("DINGTALK_WEBHOOK_URL")
        if not url:
            return None
        secret = os.getenv("AXIOM_DINGTALK_SECRET") or os.getenv("DINGTALK_SECRET")
        enabled = os.getenv("AXIOM_DINGTALK_ENABLED", "true").lower() in ("true", "1", "yes")
        return cls(
            webhook_url=url.strip(),
            secret=secret.strip() if secret else None,
            enabled=enabled,
            app_key=os.getenv("AXIOM_DINGTALK_APP_KEY") or None,
            app_secret=os.getenv("AXIOM_DINGTALK_APP_SECRET") or None,
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

        target_url = self._get_signed_url(cfg.webhook_url, cfg.secret)
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
        curl_command: str = "",
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
        if description:
            desc_preview = description.strip()
            if len(desc_preview) > 300:
                desc_preview = desc_preview[:300] + "..."
            lines.append(f"> **漏洞描述**: {desc_preview}")
        if curl_command:
            lines.append(f"**复现命令 (Curl)**:\n```bash\n{curl_command.strip()[:400]}\n```")

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
        if status_reason:
            lines.append(f"> **裁决理由**: {status_reason.strip()}")

        lines.append(f"\n*AXIOM 自动化安全测试平台 · 评审通知*")
        card_text = "\n\n".join(lines)
        return self.send_markdown(f"【漏洞评审结果】{title} -> {status}", card_text)


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
