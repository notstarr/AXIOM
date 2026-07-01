from __future__ import annotations

from datetime import datetime, timezone, timedelta

from axiom.dispatcher.tasks.maintainer import _evaluate_mozzie_tool_calls


def _item(
    *,
    tool_id: str,
    created_at: datetime,
    status: str = "success",
    rc: int = 0,
    stdout: str = '{"success": true, "data": {"id": 1}}',
) -> dict:
    return {
        "id": tool_id,
        "created_at": created_at.isoformat().replace("+00:00", "Z"),
        "status": status,
        "returncode": rc,
        "tool": "bash",
        "argv": [
            "bash",
            "curl -s -X POST https://mozzie.t.cn-shenzhen.aliyun.kkgroup.work/api/public/vulnerabilities -d '{}'",
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
