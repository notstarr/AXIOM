from __future__ import annotations

from datetime import datetime, timezone, timedelta

from axiom.dispatcher.tasks.maintainer import _evaluate_mozzie_tool_calls


VALID_CURL = (
    'curl -s -X POST "https://api-gateway.kkguan.com/client/app?code=ShoppingCart/Add/doAddToCart" '
    '-H "Cookie: KKGUANSESSIONIDNEW=abc" '
    '-H "X-TOKEN: token" '
    '-H "Content-Type: application/x-www-form-urlencoded" '
    '-H "Client: MiniKKV" '
    '-d \'data={"code":"ShoppingCart/Add/doAddToCart","params":{"token":"token"}}&os=WeChat&project=MiniKKV&version=2.4\''
)


def _item(
    *,
    tool_id: str,
    created_at: datetime,
    status: str = "success",
    rc: int = 0,
    stdout: str = '{"success": true, "data": {"id": 1}}',
    curl_command: str = VALID_CURL,
) -> dict:
    return {
        "id": tool_id,
        "created_at": created_at.isoformat().replace("+00:00", "Z"),
        "status": status,
        "returncode": rc,
        "tool": "bash",
        "argv": [
            "bash",
            (
                "python3 - <<'PY'\n"
                "import requests\n"
                "payload = {"
                f"'curl_command': {curl_command!r}"
                "}\n"
                "requests.post('https://mozzie.t.cn-shenzhen.aliyun.kkgroup.work/api/public/vulnerabilities', json=payload)\n"
                "PY"
            ),
        ],
        "stdout": stdout,
        "stderr": "",
    }


def test_evaluate_mozzie_tool_calls_fails_when_rejected() -> None:
    started = datetime.now(timezone.utc) - timedelta(minutes=1)
    items = [
        _item(
            tool_id="c1",
            created_at=datetime.now(timezone.utc),
            stdout='{"success": false, "error": "invalid vuln_type"}',
        )
    ]
    ok, reason = _evaluate_mozzie_tool_calls(items, started_at=started, expected_reports=1)
    assert ok is False
    assert "mozzie rejected" in reason


def test_evaluate_mozzie_tool_calls_succeeds_on_expected_count() -> None:
    started = datetime.now(timezone.utc) - timedelta(minutes=1)
    now = datetime.now(timezone.utc)
    items = [
        _item(tool_id="c1", created_at=now),
        _item(tool_id="c2", created_at=now),
    ]
    ok, reason = _evaluate_mozzie_tool_calls(items, started_at=started, expected_reports=2)
    assert ok is True
    assert "success count=2" in reason


def test_evaluate_mozzie_tool_calls_rejects_truncated_curl_command() -> None:
    started = datetime.now(timezone.utc) - timedelta(minutes=1)
    items = [
        _item(
            tool_id="c1",
            created_at=datetime.now(timezone.utc),
            curl_command='curl -s -X POST "https://api-gateway.kkguan.com/client/app?code=Order/Buynow/doBuy" -d \\',
        )
    ]
    ok, reason = _evaluate_mozzie_tool_calls(items, started_at=started, expected_reports=1)
    assert ok is False
    assert "invalid curl_command" in reason


def test_evaluate_mozzie_tool_calls_accepts_json_response_with_prefix_suffix() -> None:
    started = datetime.now(timezone.utc) - timedelta(minutes=1)
    items = [
        _item(
            tool_id="c1",
            created_at=datetime.now(timezone.utc),
            stdout='Status: 201\nResponse: {"success": true, "data": {"id": 123}}\nSuccess: True',
        )
    ]
    ok, reason = _evaluate_mozzie_tool_calls(items, started_at=started, expected_reports=1)
    assert ok is True
    assert "success count=1" in reason


def test_evaluate_mozzie_tool_calls_accepts_indirect_script_post_with_stdout_payload() -> None:
    started = datetime.now(timezone.utc) - timedelta(minutes=1)
    item = _item(tool_id="c1", created_at=datetime.now(timezone.utc))
    item["argv"] = ["bash", "python3 task_i034_mozzie.py 2>&1"]
    item["stdout"] = '{"curl_command": ' + repr(VALID_CURL) + '}\n{"success": true, "data": {"id": 123}}'
    ok, reason = _evaluate_mozzie_tool_calls([item], started_at=started, expected_reports=1)
    assert ok is True
    assert "success count=1" in reason


def test_evaluate_mozzie_tool_calls_accepts_status_code_success_stdout() -> None:
    started = datetime.now(timezone.utc) - timedelta(minutes=1)
    items = [
        _item(
            tool_id="c1",
            created_at=datetime.now(timezone.utc),
            stdout="status_code: 201\nsuccess: True\nmessage: None",
        )
    ]
    ok, reason = _evaluate_mozzie_tool_calls(items, started_at=started, expected_reports=1)
    assert ok is True
    assert "success count=1" in reason


def test_evaluate_mozzie_tool_calls_skips_verification_when_expected_reports_zero() -> None:
    started = datetime.now(timezone.utc) - timedelta(minutes=1)
    ok, reason = _evaluate_mozzie_tool_calls([], started_at=started, expected_reports=0)
    assert ok is True
    assert reason == "no external reports expected"
