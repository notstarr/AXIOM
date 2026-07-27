from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import Any

from axiom.modules.execution.schemas import ExecutionInput


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
        try:
            result = conclude(attempt_input, attempt_reason)
            validate(attempt_input, result)
            return
        except retry_errors as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise TimeoutError("阶段收口 deadline 已耗尽")
