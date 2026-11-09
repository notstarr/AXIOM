"""红队成果的钉钉通知分发。

把「红队成果 → 群消息」的翻译逻辑收在一处，三条调用路径共用：

- ``RedTeamService.report_result``（worker 上报，进入审核队列）
- ``RedTeamService.review_result``（Reviewer 直接调 API 裁决）
- ``VulnerabilityReviewService.complete`` 的红队分支（Reviewer 走 run 完成裁决）

调用方只负责在事务提交后调用，不在事务内发 HTTP；投递失败一律吞掉并记日志，
绝不因为钉钉不可用而回滚已落库的业务事实（与漏洞通知同一纪律）。
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Mapping

logger = logging.getLogger(__name__)


def _value(source: Any, key: str, default: Any = None) -> Any:
    """兼容 pydantic 模型与数据库行映射两种入参。"""

    if source is None:
        return default
    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)


def _as_list(value: Any) -> list[str]:
    if not value:
        return []
    return [str(item) for item in value if str(item).strip()]


def _notifier():
    from axiom.shared.dingtalk import dingtalk_notifier

    return dingtalk_notifier


def dispatch_result_reported(
    result: Any,
    *,
    chain_label: str = "",
    evidence_names: Iterable[str] | None = None,
    progress: tuple[int, int] | None = None,
) -> None:
    """成果上报进审核队列时推送（第 1 次推送，上报主通道）。"""

    try:
        notifier = _notifier()
        if not notifier.is_enabled:
            return
        notifier.notify_redteam_result_reported(
            task_id=str(_value(result, "task_id") or ""),
            result_id=str(_value(result, "id") or ""),
            title=str(_value(result, "title") or ""),
            result_type=str(_value(result, "result_type") or ""),
            description=str(_value(result, "description") or ""),
            principal=str(_value(result, "principal") or ""),
            tenant=str(_value(result, "tenant") or ""),
            target_asset=str(_value(result, "target_asset") or ""),
            chain_label=chain_label,
            capabilities_before=_as_list(_value(result, "capabilities_before")),
            capabilities_gained=_as_list(_value(result, "capabilities_gained")),
            points_declared=int(_value(result, "points_declared") or 0),
            evidence_names=_as_list(evidence_names),
            progress=progress,
        )
    except Exception:
        logger.exception(
            "Failed to dispatch DingTalk red-team report notification for %s",
            _value(result, "id"),
        )


def dispatch_result_reviewed(
    result: Any,
    *,
    points_awarded: int = 0,
    review_reason: str = "",
    reviewed_by: str = "",
    progress: tuple[int, int] | None = None,
) -> None:
    """成果审核裁决后推送（第 2 次推送，上报主通道）。"""

    try:
        notifier = _notifier()
        if not notifier.is_enabled:
            return
        notifier.notify_redteam_result_reviewed(
            task_id=str(_value(result, "task_id") or ""),
            result_id=str(_value(result, "id") or ""),
            title=str(_value(result, "title") or ""),
            result_type=str(_value(result, "result_type") or ""),
            status=str(_value(result, "status") or ""),
            points_awarded=int(points_awarded or 0),
            points_declared=int(_value(result, "points_declared") or 0),
            target_asset=str(_value(result, "target_asset") or ""),
            review_reason=review_reason,
            reviewed_by=reviewed_by,
            progress=progress,
        )
    except Exception:
        logger.exception(
            "Failed to dispatch DingTalk red-team review notification for %s",
            _value(result, "id"),
        )


def dispatch_activity_notice(
    result: Any,
    *,
    occurred_at: Any = None,
    target_asset: str | None = None,
    status: str = "",
) -> None:
    """业务方公示通道：另一台机器人，只推「什么时间·对什么业务·做了什么」。

    与上报主通道互不影响：主通道没配或投递失败，都不牵连本通道；
    本通道未配置 webhook 时直接静默。
    """

    try:
        notifier = _notifier()
        if not notifier.is_enabled:
            return
        notifier.notify_redteam_activity_notice(
            task_id=str(_value(result, "task_id") or ""),
            result_id=str(_value(result, "id") or ""),
            result_type=str(_value(result, "result_type") or ""),
            target_asset=str(
                target_asset if target_asset is not None else (_value(result, "target_asset") or "")
            ),
            occurred_at=occurred_at if occurred_at is not None else _value(result, "created_at"),
            status=status,
        )
    except Exception:
        logger.exception(
            "Failed to dispatch DingTalk red-team activity notice for %s",
            _value(result, "id"),
        )
