from unittest.mock import patch, MagicMock
import pytest
from fastapi.testclient import TestClient

from axiom.modules.settings.schemas import RuntimeConfig
from axiom.shared.dingtalk import DingTalkConfig, DingTalkNotifier


def test_dingtalk_signature_and_markdown_payload():
    config = DingTalkConfig(webhook_url="https://oapi.dingtalk.com/robot/send?access_token=test", secret="SEC123456", enabled=True)
    notifier = DingTalkNotifier(config)

    assert notifier.is_enabled is True
    signed_url = notifier._get_signed_url(config.webhook_url, config.secret)
    assert "timestamp=" in signed_url
    assert "sign=" in signed_url

    with patch("httpx.Client.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"errcode": 0, "errmsg": "ok"}
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp

        ok = notifier.notify_vulnerability_reported(
            task_id="t_172",
            vuln_id="v014",
            title="练习系统习题答案未授权泄露",
            severity="medium",
            vuln_type="未授权访问",
            status="待验证",
            affected_target="/api/practise/questionItems",
            description="无认证返回完整题目答案",
            curl_command="curl http://target/api/practise/questionItems",
        )
        assert ok is True
        assert mock_post.called
        sent_payload = mock_post.call_args[1]["json"]
        assert sent_payload["msgtype"] == "markdown"
        assert "t_172" in sent_payload["markdown"]["text"]
        assert "v014" in sent_payload["markdown"]["text"]
        assert "待验证" in sent_payload["markdown"]["text"]


def test_dingtalk_notify_reviewed():
    config = DingTalkConfig(webhook_url="https://oapi.dingtalk.com/robot/send?access_token=test", enabled=True)
    notifier = DingTalkNotifier(config)

    with patch("httpx.Client.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"errcode": 0, "errmsg": "ok"}
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp

        ok = notifier.notify_vulnerability_reviewed(
            task_id="t_172",
            vuln_id="v014",
            title="练习系统习题答案未授权泄露",
            severity="medium",
            status="已验证",
            status_reason="经复现确认接口未授权可直接读取答案",
            reviewed_by="reviewer",
        )
        assert ok is True
        sent_payload = mock_post.call_args[1]["json"]
        assert "已验证" in sent_payload["markdown"]["text"]
        assert "经复现确认" in sent_payload["markdown"]["text"]
