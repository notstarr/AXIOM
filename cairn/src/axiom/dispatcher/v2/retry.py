from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BootstrapRetryDecision:
    retry_count: int
    signature: str
    next_retry_epoch: float | None
    needs_hint: bool


@dataclass(frozen=True, slots=True)
class ExploreRetryPolicy:
    max_attempts: int = 4
    max_execute_seconds: int = 1800
    max_conclude_seconds: int = 900

    def can_attempt(self, retry_count: int) -> bool:
        return retry_count < self.max_attempts

    def execute_timeout(self, base_seconds: int, retry_count: int) -> int:
        multiplier = min(2**retry_count, 4)
        return min(base_seconds * multiplier, max(base_seconds, self.max_execute_seconds))

    def conclude_timeout(self, base_seconds: int, retry_count: int) -> int:
        multiplier = min(2**retry_count, 3)
        return min(base_seconds * multiplier, max(base_seconds, self.max_conclude_seconds))


@dataclass(frozen=True, slots=True)
class BootstrapRetryPolicy:
    rejection_threshold: int = 3
    cooldown_seconds: int = 30

    def after_rejection(
        self,
        *,
        previous_count: int,
        signature: str,
        now_epoch: float,
    ) -> BootstrapRetryDecision:
        count = previous_count + 1
        needs_hint = count >= self.rejection_threshold
        return BootstrapRetryDecision(
            retry_count=count,
            signature=signature,
            next_retry_epoch=None if needs_hint else now_epoch + self.cooldown_seconds,
            needs_hint=needs_hint,
        )

    def after_api_error(self, *, signature: str, now_epoch: float) -> BootstrapRetryDecision:
        return BootstrapRetryDecision(
            retry_count=1,
            signature=signature,
            next_retry_epoch=now_epoch + self.cooldown_seconds,
            needs_hint=False,
        )
