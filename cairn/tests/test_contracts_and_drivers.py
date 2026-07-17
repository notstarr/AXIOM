from __future__ import annotations

import json

import pytest

from axiom.dispatcher.contracts import (
    parse_json_output,
    validate_bootstrap_execute_payload,
    validate_explore_payload,
    validate_reason_payload,
)
from axiom.dispatcher.runtime.process import ManagedProcess
from axiom.dispatcher.workers.adapters.pi import PiDriver


def test_parse_json_output_extracts_object_from_markdown_noise() -> None:
    assert parse_json_output('result:\n```json\n{"accepted": true, "data": {}}\n```') == {
        "accepted": True,
        "data": {},
    }


def test_parse_json_output_prefers_fenced_final_json_over_earlier_examples() -> None:
    text = (
        'Example request body: {"pi_base_url":"http://127.0.0.1","pi_model":"x"}\n'
        'Final answer:\n'
        '```json\n'
        '{"accepted": true, "data": {"intents": [{"from": ["origin"], "description": "probe"}]}}\n'
        '```'
    )
    assert parse_json_output(text) == {
        "accepted": True,
        "data": {"intents": [{"from": ["origin"], "description": "probe"}]},
    }


def test_parse_json_output_prefers_wrapped_accepted_object_over_nested_objects() -> None:
    text = (
        "Reasoning summary before final payload.\n"
        "```json\n"
        '{"accepted": true, "data": {"intents": [{"from": ["f001"], '
        '"description": "next", "targets": ["api.example.com"], '
        '"timeouts": {"execute_seconds": 360, "conclude_seconds": 120}}]}}'
        "</｜DSML｜parameter> </｜DSML｜invoke>"
    )

    assert parse_json_output(text) == {
        "accepted": True,
        "data": {
            "intents": [
                {
                    "from": ["f001"],
                    "description": "next",
                    "targets": ["api.example.com"],
                    "timeouts": {"execute_seconds": 360, "conclude_seconds": 120},
                }
            ]
        },
    }


def test_reason_payload_limits_number_of_intents() -> None:
    kind, intents = validate_reason_payload(
        {
            "accepted": True,
            "data": {
                "intents": [
                    {
                        "from": ["f001"],
                        "description": "one",
                        "targets": ["api.example.com"],
                        "timeouts": {"execute_seconds": 120, "conclude_seconds": 45},
                    },
                    {
                        "from": ["f001"],
                        "description": "two",
                        "targets": ["pay.example.com"],
                        "timeouts": {"execute_seconds": 180, "conclude_seconds": 60},
                    },
                ]
            },
        },
        open_intents_empty=True,
        max_intents=1,
    )

    assert kind == "intents"
    assert intents == [
        {
            "from": ["f001"],
            "description": "one",
            "targets": ["api.example.com"],
            "timeouts": {"execute_seconds": 120, "conclude_seconds": 45},
        }
    ]


def test_reason_payload_requires_targets_when_enabled() -> None:
    with pytest.raises(ValueError, match="intent\\[0\\]\\.targets is required"):
        validate_reason_payload(
            {
                "accepted": True,
                "data": {"intents": [{"from": ["f001"], "description": "probe"}]},
            },
            open_intents_empty=True,
            max_intents=2,
            require_intent_targets=True,
        )


def test_reason_payload_requires_timeouts_when_enabled() -> None:
    with pytest.raises(ValueError, match="intent\\[0\\]\\.timeouts is required"):
        validate_reason_payload(
            {
                "accepted": True,
                "data": {"intents": [{"from": ["f001"], "description": "probe", "targets": ["api.example.com"]}]},
            },
            open_intents_empty=True,
            max_intents=2,
            require_intent_targets=True,
            require_intent_timeouts=True,
        )


def test_reason_payload_rejects_empty_targets() -> None:
    with pytest.raises(ValueError, match="intent\\[0\\]\\.targets must not be empty"):
        validate_reason_payload(
            {
                "accepted": True,
                "data": {"intents": [{"from": ["f001"], "description": "probe", "targets": []}]},
            },
            open_intents_empty=True,
            max_intents=2,
            require_intent_targets=True,
        )


def test_reason_payload_rejects_invalid_timeouts() -> None:
    with pytest.raises(ValueError, match="timeouts.execute_seconds must be a positive integer"):
        validate_reason_payload(
            {
                "accepted": True,
                "data": {
                    "intents": [
                        {
                            "from": ["f001"],
                            "description": "probe",
                            "targets": ["api.example.com"],
                            "timeouts": {"execute_seconds": 0, "conclude_seconds": 30},
                        }
                    ]
                },
            },
            open_intents_empty=True,
            max_intents=2,
            require_intent_targets=True,
            require_intent_timeouts=True,
        )


def test_reason_payload_requires_intent_when_none_are_open() -> None:
    kind, data = validate_reason_payload(
        {"accepted": True, "data": {}},
        open_intents_empty=True,
        max_intents=3,
    )
    assert kind == "noop"
    assert data is None


def test_explore_payload_rejects_planning_text() -> None:
    with pytest.raises(ValueError):
        validate_explore_payload(parse_json_output("Need inspect files and keep working."))


def test_explore_payload_requires_evidence_references_when_enabled() -> None:
    with pytest.raises(ValueError, match="description must reference required evidence files"):
        validate_explore_payload(
            {"accepted": True, "data": {"description": "confirmed finding only"}},
            required_reference_groups=[
                ("/tmp/axiom-files/g/p/ai/i001_findings.md", "{ai_dir}/i001_findings.md"),
                ("/tmp/axiom-files/g/p/ai/i001_leads.md", "{ai_dir}/i001_leads.md"),
            ],
        )

    kind, description = validate_explore_payload(
        {
            "accepted": True,
            "data": {
                "description": "见 {ai_dir}/i001_findings.md，未测试线索见 {ai_dir}/i001_leads.md",
            },
        },
        required_reference_groups=[
            ("/tmp/axiom-files/g/p/ai/i001_findings.md", "{ai_dir}/i001_findings.md"),
            ("/tmp/axiom-files/g/p/ai/i001_leads.md", "{ai_dir}/i001_leads.md"),
        ],
    )
    assert kind == "fact"
    assert "{ai_dir}/i001_findings.md" in description


def test_explore_payload_rejects_overlong_description_when_enabled() -> None:
    with pytest.raises(ValueError, match="description is too long"):
        validate_explore_payload(
            {"accepted": True, "data": {"description": "A" * 100}},
            max_description_chars=60,
        )

    with pytest.raises(ValueError, match="description has too many lines"):
        validate_explore_payload(
            {"accepted": True, "data": {"description": "a\nb\nc\nd"}},
            max_description_lines=3,
        )


def test_bootstrap_execute_payload_accepts_fact_only_result() -> None:
    kind, data = validate_bootstrap_execute_payload(
        {"accepted": True, "data": {"fact": {"description": "initial lead"}}}
    )

    assert kind == "fact"
    assert data == {"fact_description": "initial lead"}


def test_bootstrap_execute_payload_accepts_complete_result() -> None:
    kind, data = validate_bootstrap_execute_payload(
        {
            "accepted": True,
            "data": {
                "fact": {"description": "initial lead"},
                "complete": {"description": "goal solved"},
            },
        }
    )

    assert kind == "complete"
    assert data == {
        "fact_description": "initial lead",
        "complete_description": "goal solved",
    }


def test_pi_driver_extracts_session_and_last_assistant_text() -> None:
    driver = PiDriver()
    stdout = "\n".join(
        [
            json.dumps({"type": "session", "id": "session-123"}),
            json.dumps(
                {
                    "type": "turn_end",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": '{"accepted":true,"data":{}}'}],
                    },
                }
            ),
        ]
    )

    assert driver.extract_session(None, stdout, "") == "session-123"
    assert driver.extract_response_text(stdout, "") == '{"accepted":true,"data":{}}'


def test_pi_driver_extracts_text_from_stream_events() -> None:
    driver = PiDriver()
    stdout = "\n".join(
        [
            json.dumps({"type": "session", "id": "session-456"}),
            json.dumps({"type": "start", "partial": {"role": "assistant", "content": []}}),
            json.dumps({"type": "text_start", "contentIndex": 0}),
            json.dumps({"type": "text_delta", "contentIndex": 0, "delta": '{"accepted":'}),
            json.dumps({"type": "text_delta", "contentIndex": 0, "delta": 'true,"data":{}}'}),
            json.dumps(
                {
                    "type": "done",
                    "reason": "stop",
                    "message": {"role": "assistant", "content": [{"type": "text", "text": '{"accepted":true,"data":{}}'}]},
                }
            ),
        ]
    )

    assert driver.extract_session(None, stdout, "") == "session-456"
    assert driver.extract_response_text(stdout, "") == '{"accepted":true,"data":{}}'


def test_pi_driver_extracts_text_from_message_update_wrapper() -> None:
    driver = PiDriver()
    stdout = "\n".join(
        [
            json.dumps({"type": "session", "id": "session-789"}),
            json.dumps({"type": "message_start", "message": {"role": "assistant", "content": []}}),
            json.dumps(
                {
                    "type": "message_update",
                    "assistantMessageEvent": {
                        "type": "text_end",
                        "contentIndex": 0,
                        "content": '{"accepted":true,"data":{}}',
                        "partial": {"role": "assistant", "content": [{"type": "text", "text": '{"accepted":true,"data":{}}'}]},
                    },
                    "message": {"role": "assistant", "content": [{"type": "text", "text": '{"accepted":true,"data":{}}'}]},
                }
            ),
        ]
    )

    assert driver.extract_session(None, stdout, "") == "session-789"
    assert driver.extract_response_text(stdout, "") == '{"accepted":true,"data":{}}'


def test_pi_driver_prefers_assistant_error_over_user_prompt_examples() -> None:
    driver = PiDriver()
    stdout = "\n".join(
        [
            json.dumps(
                {
                    "type": "message",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "When rejecting output exactly "
                                    '{"accepted": false, "reason": "policy_refusal"}'
                                ),
                            }
                        ],
                    },
                }
            ),
            json.dumps(
                {
                    "type": "message",
                    "message": {
                        "role": "assistant",
                        "content": [],
                        "stopReason": "error",
                        "errorMessage": "Connection error.",
                    },
                }
            ),
        ]
    )

    assert driver.extract_response_text(stdout, "") == "Connection error."


