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
    occurred_at: Any = None,
    public_only: bool | None = None,
    target_asset: str | None = None,
) -> None:
    """成果上报进审核队列时推送（第 1 次推送）。

    ``occurred_at`` 是成果发生时间，用于业务方按告警时刻对号；缺省时卡片不编造时间。
    ``public_only`` 为真时只发业务方简版卡（时间·业务·动作），不带内部细节。
    ``target_asset`` 可指定对外公示的业务对象；留空则用成果自身的对象字段。
    """

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
            target_asset=str(
                target_asset if target_asset is not None else (_value(result, "target_asset") or "")
            ),
            chain_label=chain_label,
            capabilities_before=_as_list(_value(result, "capabilities_before")),
            capabilities_gained=_as_list(_value(result, "capabilities_gained")),
            points_declared=int(_value(result, "points_declared") or 0),
            evidence_names=_as_list(evidence_names),
            progress=progress,
            occurred_at=occurred_at if occurred_at is not None else _value(result, "created_at"),
            public_only=public_only,
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
    occurred_at: Any = None,
    public_only: bool | None = None,
) -> None:
    """成果审核裁决后推送（第 2 次推送）。"""

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
            occurred_at=occurred_at if occurred_at is not None else _value(result, "created_at"),
            public_only=public_only,
        )
    except Exception:
        logger.exception(
            "Failed to dispatch DingTalk red-team review notification for %s",
            _value(result, "id"),
        )
