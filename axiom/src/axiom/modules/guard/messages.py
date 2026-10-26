"""Model-facing wording for a blocked tool call.

The audit record keeps the raw reason (``"修改或重置用户密码"``).  What the
Worker sees is this framed version instead, because the bare reason reads
exactly like a target-side WAF response — and a red-team agent's instinct at a
WAF is to defeat it: rewrite the command, re-encode the payload, retry.  That is
both futile (the platform blocks a class of action, not one string) and wrong
(a policy decision is not an obstacle to overcome).

So the message states plainly where the block came from and that the operation
is forbidden, which lets the Worker pivot to a different technique instead of
evading this one.  It deliberately says "禁止" rather than "换一种方式": the
platform is not asking for a workaround.
"""

from __future__ import annotations


_PREFIX = "【AXIOM 平台管控·非目标防御】"
_FALLBACK = "未说明原因"


def platform_block(reason: str) -> str:
    """Frame a guard verdict as a platform decision rather than a target defense."""

    detail = (reason or "").strip() or _FALLBACK
    return f"{_PREFIX}此调用被平台拦截。原因：{detail}。此操作被禁止。"
