"""Model-facing framing of a guard block (``guard.messages``).

The Worker must not mistake a platform decision for target-side defense: the
bare reason ("修改或重置用户密码") reads exactly like a WAF response, and the
red-team instinct at a WAF is to retry with a rewritten command — which cannot
work here, because the platform blocks the class of action.
"""

from __future__ import annotations

from axiom.modules.guard.messages import platform_block


def test_a_block_is_framed_as_a_platform_decision() -> None:
    message = platform_block("修改或重置用户密码")

    assert message.startswith("【AXIOM 平台管控·非目标防御】")
    assert "修改或重置用户密码" in message
    assert "此操作被禁止" in message


def test_the_reason_survives_verbatim_inside_the_framing() -> None:
    reason = "会清空生产库"
    assert reason in platform_block(reason)


def test_a_padded_reason_is_normalized_not_dropped() -> None:
    assert "用户密码" in platform_block("  用户密码 \n")


def test_an_empty_reason_still_names_a_cause() -> None:
    """A block with no recorded reason must not render as an empty sentence."""

    message = platform_block("")
    assert "未说明原因" in message
    assert "此操作被禁止" in message

    assert "未说明原因" in platform_block("   ")


def test_the_message_never_promises_a_workaround() -> None:
    """It says the operation is forbidden, not "try another way".

    Telling the agent to find a workaround would turn a policy decision into a
    puzzle, which is the failure mode this framing exists to prevent.
    """

    message = platform_block("删除关键数据")
    assert "换一种" not in message
    assert "绕过" not in message
