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
            entry_point="GET /api/practise/questionItems",
            step_count=2,
        )
        assert ok is True
        assert mock_post.called
        sent_payload = mock_post.call_args[1]["json"]
        assert sent_payload["msgtype"] == "markdown"
        assert "t_172" in sent_payload["markdown"]["text"]
        assert "v014" in sent_payload["markdown"]["text"]
        assert "待验证" in sent_payload["markdown"]["text"]
        # 通知给入口点与步骤数，不再给一条需要自己还原的 curl。
        assert "GET /api/practise/questionItems" in sent_payload["markdown"]["text"]
        assert "2 步" in sent_payload["markdown"]["text"]
        assert "curl" not in sent_payload["markdown"]["text"].lower()


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


# ── 红队成果卡片 ──────────────────────────────────────────────────────

def _capture(notifier, method, **kwargs):
    """跑一次通知并返回实际投递的 markdown 文本。"""

    with patch("httpx.Client.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"errcode": 0, "errmsg": "ok"}
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp
        ok = getattr(notifier, method)(**kwargs)
        assert ok is True
        payload = mock_post.call_args[1]["json"]
        return payload["markdown"]["text"], payload["markdown"]["title"]


def test_redteam_result_reported_card_uses_scoring_vocabulary():
    """红队卡片用护网计分语言，不套漏洞的严重度词汇。"""

    from axiom.shared.dingtalk import DingTalkConfig, DingTalkNotifier

    notifier = DingTalkNotifier(DingTalkConfig(webhook_url="https://oapi.dingtalk.com/robot/send?access_token=test", enabled=True))
    text, title = _capture(
        notifier,
        "notify_redteam_result_reported",
        task_id="t_231",
        result_id="x019",
        title="核心应用管理员：经 userinfo-by-phone 注入冒充审核人",
        result_type="permission",
        description="伪造审核人令牌对他人的任务落地首审",
        principal="user:external",
        tenant="",
        target_asset="performance-management-api.kkguan.com（4C 业绩管理）",
        chain_label="ch000044 生产 SSO 口令面",
        capabilities_before=["user:member"],
        capabilities_gained=["app:admin"],
        points_declared=10,
        evidence_names=["request.txt", "response.txt"],
        progress=(64, 100),
    )

    assert "红队成果上报" in title
    assert "x019" in text
    assert "权限跃迁" in text            # 红队类型词汇
    assert "危害等级" not in text         # 不混用漏洞词汇
    assert "performance-management-api.kkguan.com" in text
    assert "`user:member` → `app:admin`" in text
    assert "request.txt" in text
    assert "64/100" in text              # 积分进度与 Goal 判定同口径


def test_redteam_result_reviewed_card_shows_awarded_points():
    from axiom.shared.dingtalk import DingTalkConfig, DingTalkNotifier

    notifier = DingTalkNotifier(DingTalkConfig(webhook_url="https://oapi.dingtalk.com/robot/send?access_token=test", enabled=True))
    text, _title = _capture(
        notifier,
        "notify_redteam_result_reviewed",
        task_id="t_231",
        result_id="x019",
        title="核心应用管理员",
        result_type="permission",
        status="verified",
        points_awarded=10,
        points_declared=10,
        review_reason="证据可复现，四要素齐全。",
        reviewed_by="reviewer.1",
        progress=(74, 100),
    )

    assert "已验证" in text
    assert "核定分" in text and "10" in text
    assert "证据可复现" in text
    assert "74/100" in text


def test_redteam_labels_cover_every_domain_value():
    """领域词汇必须覆盖全部枚举，避免卡片里冒出生英文。"""

    from axiom.shared.dingtalk import result_status_label, result_type_label

    for value in ("permission", "data", "credential", "pivot"):
        assert value not in result_type_label(value)
    for value in ("reported", "reviewing", "verified", "rejected", "duplicate", "needs-human"):
        assert value not in result_status_label(value)


# ── 业务方公示表达（时间·业务·动作）────────────────────────────────────


def test_redteam_card_carries_public_notice_line():
    """默认卡片正文前带一行业务方公示：什么时间·对什么业务·做了什么。"""

    from axiom.shared.dingtalk import DingTalkConfig, DingTalkNotifier

    notifier = DingTalkNotifier(
        DingTalkConfig(webhook_url="https://oapi.dingtalk.com/robot/send?access_token=test", enabled=True)
    )
    text, _title = _capture(
        notifier,
        "notify_redteam_result_reported",
        task_id="t_231",
        result_id="x001",
        title="订单越权读取",
        result_type="permission",
        description="低权限账号可读他人订单",
        target_asset="erp-web.kkgroup.work",
        points_declared=10,
        evidence_names=["packet-1"],
        occurred_at="2026-11-09T07:42:00+00:00",
    )

    assert "[红队]" in text
    assert "11-09 15:42" in text  # UTC 07:42 → 业务所在时区 15:42
    assert "erp-web.kkgroup.work" in text
    assert "越权访问" in text
    # 红队内部细节照旧保留给内部同学
    assert "低权限账号可读他人订单" in text
    assert "证据" in text


def test_redteam_public_only_card_omits_internal_details():
    """简版卡只留时间·业务·动作，不带漏洞描述、证据与积分。"""

    from axiom.shared.dingtalk import DingTalkConfig, DingTalkNotifier

    notifier = DingTalkNotifier(
        DingTalkConfig(webhook_url="https://oapi.dingtalk.com/robot/send?access_token=test", enabled=True)
    )
    text, _title = _capture(
        notifier,
        "notify_redteam_result_reported",
        task_id="t_231",
        result_id="x001",
        title="订单越权读取",
        result_type="permission",
        description="低权限账号可读他人订单",
        target_asset="erp-web.kkgroup.work",
        points_declared=10,
        evidence_names=["packet-1"],
        occurred_at="2026-11-09T07:42:00+00:00",
        public_only=True,
    )

    assert "11-09 15:42" in text
    assert "erp-web.kkgroup.work" in text
    assert "越权访问" in text
    assert "低权限账号可读他人订单" not in text
    assert "证据" not in text
    assert "自评分" not in text
    assert "packet-1" not in text


def test_redteam_public_only_follows_runtime_config():
    """运行配置打开简版时，默认就走业务方口径。"""

    from axiom.shared.dingtalk import DingTalkConfig, DingTalkNotifier

    notifier = DingTalkNotifier(
        DingTalkConfig(
            webhook_url="https://oapi.dingtalk.com/robot/send?access_token=test",
            enabled=True,
            redteam_public_only=True,
        )
    )
    text, _title = _capture(
        notifier,
        "notify_redteam_result_reported",
        task_id="t_231",
        result_id="x001",
        title="订单越权读取",
        result_type="permission",
        description="低权限账号可读他人订单",
        target_asset="erp-web.kkgroup.work",
        occurred_at="2026-11-09T07:42:00+00:00",
    )

    assert "低权限账号可读他人订单" not in text
    assert "11-09 15:42" in text


def test_redteam_review_card_also_carries_public_notice():
    """审核卡同样带公示行，业务方能看到成果状态变化对应哪个业务。"""

    from axiom.shared.dingtalk import DingTalkConfig, DingTalkNotifier

    notifier = DingTalkNotifier(
        DingTalkConfig(webhook_url="https://oapi.dingtalk.com/robot/send?access_token=test", enabled=True)
    )
    text, _title = _capture(
        notifier,
        "notify_redteam_result_reviewed",
        task_id="t_231",
        result_id="x001",
        title="订单越权读取",
        result_type="permission",
        status="verified",
        target_asset="erp-web.kkgroup.work",
        occurred_at="2026-11-09T07:42:00+00:00",
    )

    assert "[红队]" in text
    assert "erp-web.kkgroup.work" in text
    assert "越权访问" in text


def test_notice_time_uses_business_timezone_and_never_invents():
    """时间换算到业务所在时区；认不出来的原样透出，不编造时间。"""

    from axiom.shared.dingtalk import format_notice_time

    assert format_notice_time("2026-11-09T07:42:00+00:00") == "11-09 15:42"
    assert format_notice_time("2026-11-09T07:42:00Z") == "11-09 15:42"
    assert format_notice_time("2026-11-09 07:42:00+00:00") == "11-09 15:42"
    assert format_notice_time("not-a-time") == "not-a-time"
    assert format_notice_time("") == ""
    assert format_notice_time(None) == ""


def test_public_result_labels_cover_every_domain_value():
    """业务方口径的成果动作必须覆盖全部枚举，避免卡片里冒出生英文。"""

    from axiom.shared.dingtalk import public_result_type_label

    for value in ("permission", "data", "credential", "pivot"):
        label = public_result_type_label(value)
        assert value not in label
        assert label
