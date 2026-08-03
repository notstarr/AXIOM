from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from axiom.modules.token_usage.schemas import TokenUsageRecordInput, TokenUsageSourceInput


@dataclass(slots=True)
class SessionParseState:
    session_id: str = ""
    provider: str = ""
    requested_model: str = ""


@dataclass(slots=True)
class ParsedSession:
    records: list[TokenUsageRecordInput] = field(default_factory=list)
    source: TokenUsageSourceInput | None = None


def parse_session_file(path: Path, *, session_file: str | None = None) -> ParsedSession:
    relative_name = session_file or f"sessions/{path.name}"
    state = SessionParseState()
    records: list[TokenUsageRecordInput] = []
    errors: list[str] = []
    try:
        payload = path.read_bytes()
    except OSError as exc:
        return ParsedSession(
            source=TokenUsageSourceInput(
                session_file=relative_name,
                status="incomplete",
                error=f"read_error:{exc.__class__.__name__}",
            )
        )

    lines = payload.split(b"\n")
    if payload and not payload.endswith(b"\n"):
        errors.append("truncated_final_line")
    for index, raw_line in enumerate(lines, start=1):
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            errors.append(f"invalid_json_line:{index}")
            continue
        if not isinstance(event, dict):
            errors.append(f"invalid_event_line:{index}")
            continue
        record, error = parse_session_event(event, state=state, session_file=relative_name)
        if record is not None:
            records.append(record)
        if error:
            errors.append(f"{error}:{index}")

    if not state.session_id:
        errors.append("missing_session_id")

    return ParsedSession(
        records=records,
        source=TokenUsageSourceInput(
            session_file=relative_name,
            session_id=state.session_id,
            status="incomplete" if errors else "complete",
            error=";".join(dict.fromkeys(errors))[:1_000],
        ),
    )


def parse_session_event(
    event: dict[str, Any],
    *,
    state: SessionParseState,
    session_file: str,
) -> tuple[TokenUsageRecordInput | None, str]:
    event_type = str(event.get("type") or "")
    if event_type == "session":
        state.session_id = str(event.get("id") or "").strip()
        return None, ""
    if event_type == "model_change":
        state.provider = str(event.get("provider") or "").strip()
        state.requested_model = str(event.get("modelId") or "").strip()
        return None, ""

    usage: dict[str, Any] | None = None
    usage_kind = ""
    provider = state.provider
    requested_model = state.requested_model
    model = requested_model
    if event_type == "message":
        message = event.get("message")
        if not isinstance(message, dict):
            return None, ""
        role = str(message.get("role") or "")
        if role == "assistant" and isinstance(message.get("usage"), dict):
            usage = message["usage"]
            usage_kind = "assistant"
            provider = str(message.get("provider") or provider).strip()
            requested_model = str(message.get("model") or requested_model).strip()
            model = str(message.get("responseModel") or requested_model).strip()
            state.provider = provider
            state.requested_model = requested_model
        elif role == "toolResult" and isinstance(message.get("usage"), dict):
            usage = message["usage"]
            usage_kind = "tool_result"
    elif event_type in {"compaction", "branch_summary"}:
        usage = _nested_usage(event)
        if usage is not None:
            usage_kind = event_type

    if usage is None:
        return None, ""
    entry_id = str(event.get("id") or "").strip()
    timestamp = str(event.get("timestamp") or "").strip()
    if not entry_id:
        return None, "usage_missing_entry_id"
    if not timestamp:
        return None, "usage_missing_timestamp"
    normalized = normalize_usage(usage)
    if normalized is None:
        return None, "usage_invalid"
    return (
        TokenUsageRecordInput(
            session_id=state.session_id,
            session_file=session_file,
            session_entry_id=entry_id,
            usage_kind=usage_kind,  # type: ignore[arg-type]
            occurred_at=timestamp,
            provider=provider,
            model=model,
            requested_model=requested_model,
            **normalized,
        ),
        "",
    )


def normalize_usage(raw: dict[str, Any]) -> dict[str, int | None] | None:
    fields = {
        "input_tokens": ("input", "inputTokens", "input_tokens", "prompt_tokens"),
        "cache_read_tokens": (
            "cacheRead",
            "cacheReadTokens",
            "cache_read_tokens",
            "cache_read",
        ),
        "cache_write_tokens": (
            "cacheWrite",
            "cacheWriteTokens",
            "cache_write_tokens",
            "cache_write",
        ),
        "output_tokens": (
            "output",
            "outputTokens",
            "output_tokens",
            "completion_tokens",
        ),
        "reasoning_tokens": (
            "reasoning",
            "reasoning_tokens",
            "reasoningTokens",
        ),
    }
    if not any(key in raw for keys in fields.values() for key in keys):
        return None
    result: dict[str, int | None] = {}
    for destination, keys in fields.items():
        value = _first_nonnegative_int(raw, *keys)
        if value is None:
            value = 0
        result[destination] = value
    result["reported_total_tokens"] = _first_nonnegative_int(
        raw,
        "totalTokens",
        "total_tokens",
        "total",
    )
    return result


def _first_nonnegative_int(raw: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        if key not in raw:
            continue
        try:
            return max(0, int(raw[key] or 0))
        except (TypeError, ValueError):
            continue
    return None


def _nested_usage(event: dict[str, Any]) -> dict[str, Any] | None:
    direct = event.get("usage")
    if isinstance(direct, dict):
        return direct
    for key in ("compaction", "summary", "branchSummary", "branch_summary"):
        nested = event.get(key)
        if isinstance(nested, dict) and isinstance(nested.get("usage"), dict):
            return nested["usage"]
    return None
