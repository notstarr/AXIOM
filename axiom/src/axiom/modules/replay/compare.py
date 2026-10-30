"""Compare a live response against the one the worker recorded.

The reviewer's question is "did this really happen the way it was reported?".
Answering it needs comparisons we can defend, so this module only makes three
kinds of claim — status code, content type, and the presence of specific
fragments — and marks everything else ``inconclusive`` rather than guessing.

Two design consequences fall out of that:

* A comparison that cannot run is not a failure.  A missing recorded response, a
  network error, an empty body: all ``inconclusive``.  Reporting ``mismatch`` for
  those would push a reviewer toward rejecting an unreviewable result rather than
  an untrue one.
* Fragments are derived, not invented.  The tokens come from the recorded body
  itself (top-level JSON leaves, or substantive lines), so a match genuinely
  means "what was reported is what came back", not "some keyword appeared".
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from axiom.modules.replay.sender import SendResult


MAX_DERIVED_TOKENS = 5
MIN_TOKEN_LENGTH = 8

MATCH = "match"
MISMATCH = "mismatch"
INCONCLUSIVE = "inconclusive"

# Values that appear in almost any response and would make a match meaningless.
_NOISE_VALUES = frozenset(
    {"", "0", "1", "true", "false", "null", "none", "ok", "success", "200", "-1"}
)


@dataclass
class Check:
    name: str
    expected: str
    actual: str
    ok: bool | None  # None = could not compare

    def to_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "expected": self.expected,
            "actual": self.actual,
            "ok": self.ok,
        }


@dataclass
class Comparison:
    status: str = INCONCLUSIVE
    checks: list[Check] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "checks": [item.to_payload() for item in self.checks],
            "notes": list(self.notes),
        }

    @property
    def matched(self) -> int:
        return sum(1 for item in self.checks if item.ok is True)

    @property
    def failed(self) -> int:
        return sum(1 for item in self.checks if item.ok is False)

    @property
    def skipped(self) -> int:
        return sum(1 for item in self.checks if item.ok is None)


def derive_tokens(body: str) -> list[str]:
    """Pick fragments that are distinctive enough that finding them means something."""
    text = (body or "").strip()
    if not text:
        return []

    parsed = _load_json(text)
    if isinstance(parsed, dict):
        tokens = _json_leaf_tokens(parsed)
        if tokens:
            return tokens[:MAX_DERIVED_TOKENS]
    if isinstance(parsed, list) and parsed:
        tokens = _json_leaf_tokens({"items": parsed})
        if tokens:
            return tokens[:MAX_DERIVED_TOKENS]

    return _line_tokens(text)[:MAX_DERIVED_TOKENS]


def _load_json(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None


def _json_leaf_tokens(payload: dict[str, Any]) -> list[str]:
    """Top-level leaves as ``"key":value`` — recognisable in any formatting."""
    tokens: list[str] = []
    for key, value in payload.items():
        if isinstance(value, (dict, list)):
            continue
        rendered = json.dumps(value, ensure_ascii=False)
        if rendered.strip('"').lower() in _NOISE_VALUES:
            continue
        if len(rendered) < 4 and len(str(key)) < 4:
            continue
        tokens.append(f'"{key}":{rendered}')
        if len(tokens) >= MAX_DERIVED_TOKENS:
            break
    return tokens


def _line_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if len(stripped) < MIN_TOKEN_LENGTH:
            continue
        if stripped.lower() in _NOISE_VALUES:
            continue
        tokens.append(stripped[:120])
        if len(tokens) >= MAX_DERIVED_TOKENS:
            break
    return tokens


def _normalise_fragment(token: str) -> str:
    """Compare on a whitespace-collapsed form so formatting is not a mismatch."""
    return re.sub(r"\s+", "", token)


def compare(
    recorded_body: str,
    recorded_status: int | None,
    recorded_content_type: str,
    live: SendResult,
    *,
    extra_asserts: list[str] | None = None,
) -> Comparison:
    """Judge the live answer against the recorded one."""
    result = Comparison()

    if not live.sent:
        result.notes.append(f"未能发送：{live.error_kind} — {live.error_message}")
        result.status = INCONCLUSIVE
        return result

    if recorded_status is not None:
        actual_status = live.status_code or 0
        result.checks.append(
            Check(
                name="状态码",
                expected=str(recorded_status),
                actual=str(actual_status),
                ok=recorded_status == actual_status,
            )
        )
    else:
        result.checks.append(Check(name="状态码", expected="(未记录)", actual=str(live.status_code or ""), ok=None))

    live_content_type = (live.headers.get("content-type") or "").split(";")[0].strip().lower()
    recorded_ct = (recorded_content_type or "").split(";")[0].strip().lower()
    if recorded_ct:
        result.checks.append(
            Check(
                name="Content-Type",
                expected=recorded_ct,
                actual=live_content_type,
                ok=recorded_ct == live_content_type,
            )
        )
    else:
        result.checks.append(
            Check(name="Content-Type", expected="(未记录)", actual=live_content_type, ok=None)
        )

    tokens: list[str] = []
    for token in (extra_asserts or []):
        if token and token not in tokens:
            tokens.append(token)
    for token in derive_tokens(recorded_body):
        if token not in tokens:
            tokens.append(token)

    live_body = _normalise_fragment(live.body)
    if not tokens:
        result.notes.append("上报的响应体没有可提取的关键片段，未做内容比对")
    for token in tokens:
        present = _normalise_fragment(token) in live_body
        result.checks.append(
            Check(name="关键片段", expected=token, actual="出现" if present else "未出现", ok=present)
        )

    if live.body_truncated:
        result.notes.append(
            f"实际响应 {live.body_bytes} 字节，只比对前 {len(live.body.encode('utf-8'))} 字节"
        )
    if live.scheme_fell_back:
        result.notes.append(f"https 失败后回落到 {live.scheme_used}")

    result.status = _verdict(result)
    return result


def _verdict(result: Comparison) -> str:
    if result.failed:
        return MISMATCH
    if result.matched == 0:
        return INCONCLUSIVE
    return MATCH


__all__ = [
    "Check",
    "Comparison",
    "INCONCLUSIVE",
    "MATCH",
    "MISMATCH",
    "compare",
    "derive_tokens",
]
