"""One vocabulary for a finding's status.

The dashboard filters findings by status (``static/js/helpers.js`` → ``vulnStatusKey``)
and the report layer decides what belongs in the body of a task report and what belongs in
its appendix.  Two hand-written lists of the same four words drift apart the moment one of
them learns a synonym, and the drift is invisible until the report says "only confirmed
findings" while showing a pending one.

So the mapping lives here, once.  Both sides ask it the same question.
"""

from __future__ import annotations


VERIFIED = "verified"
PENDING = "pending"
IGNORED = "ignored"
DUPLICATE = "duplicate"

# The four states the schema pins, plus the spellings the dashboard has always
# understood.  A state outside this table is *unknown*, not *new*: the report keeps such a
# record in its appendix instead of dropping it, because a record that vanishes silently
# makes the cover's counts disagree with the list.
_STATUS_KEYS: dict[str, str] = {
    "已验证": VERIFIED,
    "已确认": VERIFIED,
    "verified": VERIFIED,
    "confirmed": VERIFIED,
    "待验证": PENDING,
    "待确认": PENDING,
    "待审核": PENDING,
    "pending": PENDING,
    "忽略": IGNORED,
    "已忽略": IGNORED,
    "ignored": IGNORED,
    "重复": DUPLICATE,
    "已拒绝": DUPLICATE,
    "rejected": DUPLICATE,
    "duplicate": DUPLICATE,
}


def status_key(status: object) -> str:
    """The canonical bucket for a status, or ``""`` when the state is unknown.

    Case is ignored for the English spellings only; the Chinese ones are matched exactly,
    because 「待验证」 and 「待审核」 are different words that happen to mean the same thing
    here, and neither of them means anything in another language.
    """
    raw = str(status or "").strip()
    if not raw:
        return ""
    return _STATUS_KEYS.get(raw) or _STATUS_KEYS.get(raw.lower(), "")


def is_verified(status: object) -> bool:
    """True when the finding has been confirmed and is being asked to be fixed."""
    return status_key(status) == VERIFIED


def is_pending(status: object) -> bool:
    """True when the finding exists but has not been confirmed yet."""
    return status_key(status) == PENDING


__all__ = [
    "DUPLICATE",
    "IGNORED",
    "PENDING",
    "VERIFIED",
    "is_pending",
    "is_verified",
    "status_key",
]
