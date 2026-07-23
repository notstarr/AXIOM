from __future__ import annotations

import json
from typing import Any


TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "total_tokens",
)


def extract_token_usage(stdout: str) -> dict[str, int] | None:
    """Extract one PI invocation's usage without counting streamed duplicates twice.

    PI emits assistant messages while running and repeats the same messages in the
    final ``agent_end`` event.  The final event is authoritative because its
    ``messages`` collection contains every model call made by that invocation.
    Older providers that do not emit ``agent_end`` keep the previous best-candidate
    fallback behavior.
    """

    events = _json_line_events(stdout)
    for event in reversed(events):
        if event.get("type") != "agent_end" or not isinstance(event.get("messages"), list):
            continue
        usages = [
            usage
            for message in event["messages"]
            if isinstance(message, dict)
            for usage in [_normalize_usage_map(message.get("usage"))]
            if usage is not None
        ]
        if usages:
            return sum_token_usage(usages)

    candidates = [
        usage
        for event in events
        for usage in _find_usage_candidates(event)
        if usage is not None
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda item: item["total_tokens"])


def sum_token_usage(usages: list[dict[str, int] | None]) -> dict[str, int] | None:
    totals = {field: 0 for field in TOKEN_FIELDS}
    found = False
    for usage in usages:
        if not usage:
            continue
        found = True
        for field in TOKEN_FIELDS:
            totals[field] += max(0, int(usage.get(field, 0) or 0))
    if not found:
        return None
    return {field: value for field, value in totals.items() if value or field in {"input_tokens", "output_tokens", "total_tokens"}}


def _json_line_events(stdout: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        raw = line.strip()
        if not raw or not (raw.startswith("{") and raw.endswith("}")):
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events


def _find_usage_candidates(value: Any) -> list[dict[str, int] | None]:
    hits: list[dict[str, int] | None] = []
    if isinstance(value, dict):
        parsed = _normalize_usage_map(value)
        if parsed is not None:
            hits.append(parsed)
        for nested in value.values():
            hits.extend(_find_usage_candidates(nested))
    elif isinstance(value, list):
        for item in value:
            hits.extend(_find_usage_candidates(item))
    return hits


def _normalize_usage_map(payload: Any) -> dict[str, int] | None:
    if not isinstance(payload, dict):
        return None
    input_tokens = _to_non_negative_int(
        payload.get("input_tokens", payload.get("prompt_tokens", payload.get("inputTokens", payload.get("input"))))
    )
    output_tokens = _to_non_negative_int(
        payload.get("output_tokens", payload.get("completion_tokens", payload.get("outputTokens", payload.get("output"))))
    )
    cache_read_tokens = _to_non_negative_int(
        payload.get("cache_read_tokens", payload.get("cached_tokens", payload.get("cacheRead", payload.get("cache_read"))))
    )
    cache_write_tokens = _to_non_negative_int(
        payload.get("cache_write_tokens", payload.get("cacheWrite", payload.get("cache_write")))
    )
    reasoning_tokens = _to_non_negative_int(
        payload.get("reasoning_tokens", payload.get("reasoningTokens", payload.get("reasoning")))
    )
    total_tokens = _to_non_negative_int(payload.get("total_tokens", payload.get("totalTokens")))
    if all(
        value is None
        for value in (
            input_tokens,
            output_tokens,
            cache_read_tokens,
            cache_write_tokens,
            reasoning_tokens,
            total_tokens,
        )
    ):
        return None
    input_tokens = input_tokens or 0
    output_tokens = output_tokens or 0
    cache_read_tokens = cache_read_tokens or 0
    cache_write_tokens = cache_write_tokens or 0
    reasoning_tokens = reasoning_tokens or 0
    if total_tokens is None:
        total_tokens = input_tokens + output_tokens + cache_read_tokens + cache_write_tokens
    result = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }
    if cache_read_tokens:
        result["cache_read_tokens"] = cache_read_tokens
    if cache_write_tokens:
        result["cache_write_tokens"] = cache_write_tokens
    if reasoning_tokens:
        result["reasoning_tokens"] = reasoning_tokens
    return result


def _to_non_negative_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        parsed = int(value)
        return parsed if parsed >= 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None
