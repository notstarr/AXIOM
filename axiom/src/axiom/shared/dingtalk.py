from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DingTalkConfig:
    webhook_url: str
    secret: str | None = None
    enabled: bool = True

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
                )
        except Exception:
            pass

        url = os.getenv("AXIOM_DINGTALK_WEBHOOK_URL") or os.getenv("DINGTALK_WEBHOOK_URL")
        if not url:
            return None
        secret = os.getenv("AXIOM_DINGTALK_SECRET") or os.getenv("DINGTALK_SECRET")
        enabled = os.getenv("AXIOM_DINGTALK_ENABLED", "true").lower() in ("true", "1", "yes")
        return cls(webhook_url=url.strip(), secret=secret.strip() if secret else None, enabled=enabled)


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
            with httpx.Client(timeout=10.0) as client:
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
