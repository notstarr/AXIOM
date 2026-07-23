from __future__ import annotations

from axiom.dispatcher.token_usage import extract_token_usage


def test_extract_token_usage_from_agent_end_payload() -> None:
    stdout = """{"type":"agent_end","usage":{"input_tokens":123,"output_tokens":45,"total_tokens":168}}"""
    usage = extract_token_usage(stdout)
    assert usage == {"input_tokens": 123, "output_tokens": 45, "total_tokens": 168}


def test_extract_token_usage_supports_prompt_completion_keys() -> None:
    stdout = """{"prompt_tokens":10,"completion_tokens":5}\n{"message":"done"}"""
    usage = extract_token_usage(stdout)
    assert usage == {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}


def test_extract_token_usage_returns_none_when_missing() -> None:
    stdout = """{"type":"log","message":"no usage"}"""
    assert extract_token_usage(stdout) is None


def test_extract_token_usage_sums_messages_from_agent_end_without_double_counting() -> None:
    stdout = "\n".join(
        [
            '{"type":"message_end","message":{"role":"assistant","usage":{"input":10,"output":2,"totalTokens":12}}}',
            '{"type":"agent_end","messages":['
            '{"role":"assistant","usage":{"input":10,"output":2,"totalTokens":12}},'
            '{"role":"toolResult"},'
            '{"role":"assistant","usage":{"input":20,"output":3,"cacheRead":4,"totalTokens":27}}'
            "]}",
        ]
    )

    assert extract_token_usage(stdout) == {
        "input_tokens": 30,
        "output_tokens": 5,
        "cache_read_tokens": 4,
        "total_tokens": 39,
    }
