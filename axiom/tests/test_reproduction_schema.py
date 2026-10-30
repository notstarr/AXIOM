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


def test_a_curl_command_is_translated_into_a_packet() -> None:
    """Workers paste curl because that is what they typed; it becomes a packet.

    Rejecting it only pushed the hand-expansion onto the worker, which is where
    headers get dropped — so the command is translated instead, and what gets stored
    is a packet that could really have been sent.
    """
    reproduction = parse_reproduction(
        json.dumps(
            _payload(
                step={
                    "request": 'curl -X POST https://target.test/upload.php '
                    '-H "Cookie: s=1" -d "a=1"'
                }
            )
        )
    )
    stored = reproduction.steps[0].exchanges[0].request
    assert stored.startswith("POST /upload.php HTTP/1.1")
    assert "Host: target.test" in stored
    assert "Cookie: s=1" in stored
    assert "curl" not in stored


def test_a_curl_command_that_cannot_be_translated_is_rejected_by_name() -> None:
    """Silently dropping ``--cert`` would describe an anonymous request."""
    with pytest.raises(ValueError) as excinfo:
        parse_reproduction(
            json.dumps(_payload(step={"request": "curl --cert c.pem https://target.test/x"}))
        )
    assert "--cert" in str(excinfo.value)


@pytest.mark.parametrize("opener", ["python3 poc.py", "httpx get http://x/", "wget http://x/"])
def test_non_curl_shell_commands_are_still_rejected(opener: str) -> None:
    with pytest.raises(ValueError, match="标准 HTTP 请求报文"):
        parse_reproduction(json.dumps(_payload(step={"request": opener})))


def test_a_prompted_curl_paste_is_translated_too() -> None:
    """A copied transcript keeps the ``$`` prompt; it is not part of the command."""
    reproduction = parse_reproduction(
        json.dumps(_payload(step={"request": "$ curl -s http://target.test/x"}))
    )
    assert reproduction.steps[0].exchanges[0].request.startswith("GET /x HTTP/1.1")


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


# ------------------------------------------------- one step, several packets

TWO_EXCHANGES = {
    "title": "申领凭证并覆盖他人客户文件",
    "exchanges": [
        {
            "request": "POST /admin/v1/file/temporary-credential HTTP/1.1\r\nHost: e.test\r\n\r\n{}",
            "response": "HTTP/1.1 200 OK\r\n\r\n{\"code\":1000}",
        },
        {
            "request": "POST https://oss.test/ HTTP/1.1\r\n\r\nkey=000/import/poc.xlsx",
            "response": "HTTP/1.1 200 OK\r\n\r\n{\"code\":1000}",
        },
    ],
    "proves": "仅用出库凭证即可覆盖他人客户导入文件",
}


def test_a_step_can_carry_several_packets() -> None:
    """两个请求才能成立的结论，不该被挤进同一个 request 字段。

    拼在一起渲染出来的是一个从未真实存在的报文：读者分不清哪行响应对应哪个请求，
    重放工具也只能按一个请求发。
    """
    reproduction = parse_reproduction(json.dumps(_payload(steps=[TWO_EXCHANGES])))

    step = reproduction.steps[0]
    assert len(step.exchanges) == 2
    assert step.exchanges[0].request.startswith("POST /admin/v1/file/temporary-credential")
    assert step.exchanges[1].request.startswith("POST https://oss.test/")
    # 首组仍可通过 request/response 读到，重放器与旧读模型不必改
    assert step.request == step.exchanges[0].request
    assert step.response == step.exchanges[0].response


def test_the_single_packet_shape_is_still_accepted_and_normalized() -> None:
    """存量行、以及只打了一个请求的步骤，都写的是 request/response。"""
    reproduction = parse_reproduction(json.dumps(_payload()))

    step = reproduction.steps[0]
    assert len(step.exchanges) == 1
    assert step.exchanges[0].request.startswith("POST /upload.php HTTP/1.1")
    # 归一化后存的是 exchanges；旧字段不再写回，避免同一份报文存两遍
    dumped = reproduction.model_dump()
    assert "request" not in dumped["steps"][0]
    assert dumped["steps"][0]["exchanges"][0]["request"].startswith("POST /upload.php")


def test_mixing_both_shapes_in_one_step_is_rejected() -> None:
    """两种写法并用只会有一个结果：其中一组被静默丢掉。"""
    with pytest.raises(ValueError, match="不能同时"):
        parse_reproduction(
            json.dumps(
                _payload(
                    step={
                        "exchanges": [{"request": VALID_REQUEST, "response": VALID_RESPONSE}],
                        "request": VALID_REQUEST,
                        "response": VALID_RESPONSE,
                    }
                )
            )
        )


def test_a_step_with_no_packet_is_rejected() -> None:
    """没有报文的步骤不是复现。空 exchanges 与空报文都必须被拒。"""
    empty_step = {"title": "什么都没打", "exchanges": [], "proves": "空"}
    with pytest.raises(ValueError, match="至少要有一组"):
        parse_reproduction(json.dumps(_payload(steps=[empty_step])))
    with pytest.raises(ValueError, match="请求报文不能为空"):
        parse_reproduction(json.dumps(_payload(step={"request": "", "response": ""})))


def test_too_many_packets_in_one_step_is_rejected() -> None:
    from axiom.modules.reproduction import MAX_EXCHANGES_PER_STEP

    exchanges = [{"request": VALID_REQUEST, "response": VALID_RESPONSE}] * (
        MAX_EXCHANGES_PER_STEP + 1
    )

    with pytest.raises(ValueError, match=f"最多 {MAX_EXCHANGES_PER_STEP} 组"):
        parse_reproduction(
            json.dumps(_payload(steps=[{**TWO_EXCHANGES, "exchanges": exchanges}]))
        )


def test_every_packet_in_a_step_is_validated_not_just_the_first() -> None:
    """第二组写错了也必须报错：漏校验等于允许半个报文混进去。

    用一个不会被自动转换的 shell 命令做样本——curl 现在会被翻译成报文，不再是错误。
    """
    with pytest.raises(ValueError, match="标准 HTTP 请求报文"):
        parse_reproduction(
            json.dumps(
                _payload(
                    steps=[
                        {
                            **TWO_EXCHANGES,
                            "exchanges": [
                                TWO_EXCHANGES["exchanges"][0],
                                {"request": "python3 poc.py", "response": VALID_RESPONSE},
                            ],
                        }
                    ]
                )
            )
        )
