from __future__ import annotations

from cairn.dispatcher.tasks.common import _extract_token_usage


def test_extract_token_usage_from_agent_end_payload() -> None:
    stdout = """{"type":"agent_end","usage":{"input_tokens":123,"output_tokens":45,"total_tokens":168}}"""
    usage = _extract_token_usage(stdout)
    assert usage == {"input_tokens": 123, "output_tokens": 45, "total_tokens": 168}


def test_extract_token_usage_supports_prompt_completion_keys() -> None:
    stdout = """{"prompt_tokens":10,"completion_tokens":5}\n{"message":"done"}"""
    usage = _extract_token_usage(stdout)
    assert usage == {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}


def test_extract_token_usage_returns_none_when_missing() -> None:
    stdout = """{"type":"log","message":"no usage"}"""
    assert _extract_token_usage(stdout) is None
