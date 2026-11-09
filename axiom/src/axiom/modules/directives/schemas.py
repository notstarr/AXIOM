"""Wire shapes for human-issued worker directives.

The bundle is deliberately a separate field on ExecutionInput rather than a
section of ``injected_system_context``: that text is derived from the context
manifest, carries a content hash and source refs, and is withheld from conclude.
Directives have their own delivery rules and must not be可回收 as context.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


DirectiveScope = Literal[
    "reason_all", "reason_next", "explore_all", "explore_one", "all_workers"
]
DirectiveLifetime = Literal["persistent", "once"]
DirectiveStatus = Literal["pending", "active", "delivered", "disabled", "expired"]
DeliveryKind = Literal["reason_next", "explore_one"]
DeliveryStatus = Literal["queued", "delivered", "reopened"]

PERSISTENT_SCOPES = frozenset({"reason_all", "explore_all", "all_workers"})
ONCE_SCOPES = frozenset({"reason_next", "explore_one"})

_BEGIN = "--- BEGIN AXIOM WORKER DIRECTIVES ---"
_END = "--- END AXIOM WORKER DIRECTIVES ---"
_PRIORITY = (
    "以下是人工下发的任务级 Worker 指令。优先级：系统规则 / Scope / 阶段合同 "
    "> Worker 指令 > Project Knowledge > Skill 方法论。\n"
    "指令只作为阶段输入，不能覆盖系统规则、Scope、阶段合同或工具权限。"
)


class DirectiveItem(BaseModel):
    id: str
    content: str


class WorkerDirectiveBundle(BaseModel):
    """What one execution input is allowed to see. Empty by default."""

    all_workers: list[DirectiveItem] = Field(default_factory=list)
    reason_all: list[DirectiveItem] = Field(default_factory=list)
    explore_all: list[DirectiveItem] = Field(default_factory=list)
    reason_once: list[DirectiveItem] = Field(default_factory=list)
    explore_once: list[DirectiveItem] = Field(default_factory=list)

    @staticmethod
    def _lines(items: list[DirectiveItem]) -> list[str]:
        return [f"- [{item.id}] {item.content}" for item in items]

    def render_system_block(self) -> str:
        """Goes into the shared system-prompt prefix (execute + conclude)."""
        if not self.all_workers:
            return ""
        body = "\n".join(
            ["## 对全部 Worker（常驻生效）", *self._lines(self.all_workers)]
        )
        return f"{_BEGIN}\n{_PRIORITY}\n{body}\n{_END}"

    def render_reason_section(self) -> str:
        """Goes into reason.md / reason_redteam.md (execute only)."""
        parts: list[str] = []
        if self.reason_all:
            parts += ["### 对全体 Reason 的常驻指令", *self._lines(self.reason_all)]
        if self.reason_once:
            parts += [
                "### 本轮一次性指令（仅本次 Reason 生效）",
                *self._lines(self.reason_once),
            ]
        if not parts:
            return ""
        return f"{_BEGIN}\n{_PRIORITY}\n" + "\n".join(parts) + f"\n{_END}"

    def render_explore_section(self) -> str:
        """Goes into explore.md / explore_redteam.md (execute only)."""
        parts: list[str] = []
        if self.explore_all:
            parts += ["### 对全体 Explore 的常驻指令", *self._lines(self.explore_all)]
        if self.explore_once:
            parts += [
                "### 本次 Explore 的一次性指令（仅本次生效）",
                *self._lines(self.explore_once),
            ]
        if not parts:
            return ""
        return f"{_BEGIN}\n{_PRIORITY}\n" + "\n".join(parts) + f"\n{_END}"


class CreateDirective(BaseModel):
    content: str = Field(min_length=1, max_length=8_000)
    creator: str = "human"
    scope: DirectiveScope
    target_intent_id: str | None = None
    client_message_id: str | None = Field(default=None, max_length=160)
    expires_at: str | None = None

    @model_validator(mode="after")
    def target_matches_scope(self) -> "CreateDirective":
        if self.scope == "explore_one" and not (self.target_intent_id or "").strip():
            raise ValueError("explore_one 指令必须指定 target_intent_id")
        if self.scope != "explore_one" and self.target_intent_id:
            raise ValueError("只有 explore_one 指令可以指定 target_intent_id")
        return self


class UpdateDirective(BaseModel):
    content: str | None = Field(default=None, min_length=1, max_length=8_000)
    disabled: bool | None = None
    expires_at: str | None = None


class DeliverySummary(BaseModel):
    id: str
    delivery_kind: DeliveryKind
    reason_run_id: str | None = None
    intent_id: str | None = None
    lease_id: str | None = None
    status: DeliveryStatus
    created_at: str


class Directive(BaseModel):
    id: str
    task_id: str
    content: str
    creator: str
    scope: DirectiveScope
    lifetime: DirectiveLifetime
    target_intent_id: str | None = None
    status: DirectiveStatus
    client_message_id: str | None = None
    created_at: str
    updated_at: str | None = None
    delivered_at: str | None = None
    delivered_run_id: str | None = None
    disabled_at: str | None = None
    expires_at: str | None = None
    archived: bool = False
    deliveries: list[DeliverySummary] = Field(default_factory=list)


class EligibleExplore(BaseModel):
    intent_id: str
    description: str = ""
    targets: list[str] = Field(default_factory=list)
    created_at: str = ""
