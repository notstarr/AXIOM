"""Structured reproduction validation.

The point of these rules is that a report consumer pastes the packet into a proxy and
sees what the tester saw.  A curl line or an elided response defeats that, so those
are rejected rather than quietly accepted.
"""

from __future__ import annotations

import json

import pytest

from axiom.modules.reproduction import (
    MAX_PACKET_BYTES,
    MAX_STEPS,
    Reproduction,
    parse_reproduction,
)


VALID_REQUEST = (
    "POST /upload.php HTTP/1.1\r\n"
    "Host: target.test\r\n"
    "Content-Type: multipart/form-data; boundary=----X\r\n"
    "\r\n"
    "------X\r\n"
    'Content-Disposition: form-data; name="avatar"; filename="shell.php"\r\n'
    "Content-Type: application/x-php\r\n"
    "\r\n"
    "<?php echo 'ok'; ?>\r\n"
    "------X--\r\n"
)
VALID_RESPONSE = "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{\"uploaded\":\"/uploads/shell.php\"}"


def _payload(**overrides) -> dict:
    step = {
        "title": "上传 PHP 文件到头像接口",
        "request": VALID_REQUEST,
        "response": VALID_RESPONSE,
        "proves": "上传接口不校验类型，任意 .php 落地并可直接访问",
    }
    step.update(overrides.pop("step", {}))
    payload = {
        "entry_point": "POST /upload.php",
        "prerequisites": ["无需认证", "  ", "无需要登录"],
        "steps": [step],
    }
    payload.update(overrides)
    return payload


def test_a_realistic_reproduction_parses_and_normalizes() -> None:
    reproduction = parse_reproduction(json.dumps(_payload()))

    assert reproduction.entry_point == "POST /upload.php"
    # Blank entries dropped, duplicates collapsed: the report renders one bullet each.
    assert reproduction.prerequisites == ["无需认证", "无需要登录"]
    assert len(reproduction.steps) == 1
    assert reproduction.steps[0].request.startswith("POST /upload.php HTTP/1.1")


def test_a_curl_command_is_rejected_with_the_rewrite_instruction() -> None:
    with pytest.raises(ValueError) as excinfo:
        parse_reproduction(
            json.dumps(
                _payload(
                    step={
                        "request": 'curl -X POST https://target.test/upload.php -F "avatar=@shell.php"'
                    }
                )
            )
        )

    message = str(excinfo.value)
    assert "request" in message
    assert "标准 HTTP 请求报文" in message


@pytest.mark.parametrize("opener", ["$ curl -s http://x/", "python3 poc.py", "httpx get http://x/"])
def test_shell_openers_are_rejected(opener: str) -> None:
    with pytest.raises(ValueError, match="标准 HTTP 请求报文"):
        parse_reproduction(json.dumps(_payload(step={"request": opener})))


def test_a_request_without_a_request_line_is_rejected() -> None:
    with pytest.raises(ValueError) as excinfo:
        parse_reproduction(json.dumps(_payload(step={"request": "GET /only-path-without-version"})))

    assert "首行必须形如" in str(excinfo.value)


def test_a_response_without_a_status_line_is_rejected() -> None:
    with pytest.raises(ValueError) as excinfo:
        parse_reproduction(json.dumps(_payload(step={"response": "{\"code\": 1000}"})))

    assert "HTTP 状态行" in str(excinfo.value)


def test_an_elided_response_is_not_silently_accepted() -> None:
    """An empty response means the worker could not show the proof, which is the
    whole reason the field exists."""
    with pytest.raises(ValueError, match="响应不能为空"):
        parse_reproduction(json.dumps(_payload(step={"response": "   "})))


def test_an_oversized_packet_is_rejected_and_says_what_to_do() -> None:
    oversized = VALID_REQUEST + "A" * MAX_PACKET_BYTES

    with pytest.raises(ValueError) as excinfo:
        parse_reproduction(json.dumps(_payload(step={"request": oversized})))

    assert "只保留能证明问题的关键" in str(excinfo.value)


def test_more_steps_than_the_cap_are_rejected() -> None:
    steps = [
        {
            "title": f"第 {index} 步",
            "request": VALID_REQUEST,
            "response": VALID_RESPONSE,
            "proves": "同质步骤",
        }
        for index in range(MAX_STEPS + 1)
    ]

    with pytest.raises(ValueError) as excinfo:
        parse_reproduction(json.dumps(_payload(steps=steps)))

    assert f"最多 {MAX_STEPS} 步" in str(excinfo.value)


def test_no_steps_is_rejected() -> None:
    with pytest.raises(ValueError, match="至少要有一个复现步骤"):
        parse_reproduction(json.dumps(_payload(steps=[])))


def test_a_step_without_title_or_proves_is_rejected() -> None:
    with pytest.raises(ValueError, match="title"):
        parse_reproduction(json.dumps(_payload(step={"title": "  "})))
    with pytest.raises(ValueError, match="proves"):
        parse_reproduction(json.dumps(_payload(step={"proves": "  "})))


def test_truncated_json_reports_the_line_not_a_bare_decoder_error() -> None:
    truncated = json.dumps(_payload())[:-5]

    with pytest.raises(ValueError) as excinfo:
        parse_reproduction(truncated)

    assert "解析失败" in str(excinfo.value)
    assert "\\n 转义" in str(excinfo.value)


def test_unknown_fields_are_rejected_so_typos_do_not_vanish() -> None:
    with pytest.raises(ValueError, match="chain_gian"):
        parse_reproduction(json.dumps(_payload(chain_gian="typo")))


def test_chain_increment_fields_survive_a_round_trip() -> None:
    reproduction = parse_reproduction(
        json.dumps(
            _payload(
                chain_input="  已持有 v004 面板 token  ",
                chain_gain="拿到 192.168.0.95 shell",
            )
        )
    )

    assert reproduction.chain_input == "已持有 v004 面板 token"
    assert reproduction.chain_gain == "拿到 192.168.0.95 shell"
    # The attack path is assembled from the graph, never stored here.
    assert reproduction.model_dump()["steps"][0]["proves"]


def test_a_reproduction_over_the_whole_document_cap_is_rejected() -> None:
    # Each packet is under the per-packet cap; six of them blow the document cap.
    big_response = "HTTP/1.1 200 OK\r\n\r\n" + "B" * 12_000
    steps = [
        {
            "title": f"第 {index} 步",
            "request": VALID_REQUEST,
            "response": big_response,
            "proves": "填充到超过整份上限",
        }
        for index in range(6)
    ]

    with pytest.raises(ValueError) as excinfo:
        parse_reproduction(json.dumps(_payload(steps=steps)))

    assert "超过上限" in str(excinfo.value)


def test_reproduction_model_rejects_a_command_in_any_step_not_just_the_first() -> None:
    payload = _payload()
    payload["steps"] = [
        payload["steps"][0],
        {
            "title": "第二步用 sqlmap",
            "request": "sqlmap -u http://target.test/?id=1 --batch",
            "response": VALID_RESPONSE,
            "proves": "注入",
        },
    ]

    with pytest.raises(ValueError) as excinfo:
        Reproduction.model_validate(payload)

    assert "steps.1.request" in str(excinfo.value) or "steps" in str(excinfo.value)
