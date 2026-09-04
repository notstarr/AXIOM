from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import Any

from axiom.modules.execution.schemas import ActivityCommand, ExecutionInput


def make_closeout_progress(activity, work, *, verb: str, kind: str = "concluding"):
    """Build a best-effort activity reporter for bounded_closeout attempts.

    收口窗口里 agent 的活动上报是被禁用的，这里由 dispatcher 侧代报，
    让「收尾中」卡片显示当前进行到第几次收口以及上次的校验失败原因。
    """

    def progress(attempt: int, max_attempts: int, note: str) -> None:
        summary = f"{verb}（第 {attempt}/{max_attempts} 次）"
        if note:
            trimmed = note if len(note) <= 120 else note[:117] + "..."
            summary += f"，上一轮校验未过：{trimmed}"
        try:
            activity(
                work,
                ActivityCommand(
                    lease_id=work.lease_id,
                    activity_kind=kind,
                    activity_summary=summary,
                    activity_tool="",
                ),
            )
        except Exception:
            pass

    return progress


def bounded_closeout(
    *,
    execution_input: ExecutionInput,
    begin: Callable[[], None],
    conclude: Callable[[ExecutionInput, str], Any],
    validate: Callable[[ExecutionInput, Any], None],
    reason: str,
    retry_errors: tuple[type[BaseException], ...],
    default_timeout_seconds: int,
    max_attempts: int = 3,
    progress: Callable[[int, int, str], None] | None = None,
) -> None:
    """Retry only contract closeout, within one shared deadline and session."""
    budget = max(1, execution_input.conclude_timeout_seconds or default_timeout_seconds)
    deadline = time.monotonic() + budget
    begin()
    last_error: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        remaining = math.ceil(deadline - time.monotonic())
        if remaining <= 0:
            break
        attempt_input = execution_input.model_copy(
            update={"conclude_timeout_seconds": remaining}
        )
        attempt_reason = reason
        if attempt > 1:
            attempt_reason = (
                f"{reason}；这是第 {attempt}/{max_attempts} 次收口。"
                f"上一次校验错误：{last_error}。只做最小补齐，最后一条 assistant 文本必须"
                "严格只输出 FINISH，不要输出解释、Markdown 或代码块。"
            )
        if progress is not None:
            progress(
                attempt,
                max_attempts,
                "" if last_error is None else str(last_error),
            )
        try:
            result = conclude(attempt_input, attempt_reason)
            validate(attempt_input, result)
            return
        except retry_errors as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise TimeoutError("阶段收口 deadline 已耗尽")
