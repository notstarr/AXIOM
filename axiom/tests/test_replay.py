"""Replay: packet parsing, safety gates, comparison, and rendering.

The safety tests matter most.  This service is the one place in AXIOM that will send
an arbitrary HTTP request on demand, so "does it refuse the wrong targets" is not a
detail — it is the reason the feature is allowed to exist.
"""

from __future__ import annotations

import base64

import pytest

from axiom.modules.replay.compare import (
    INCONCLUSIVE,
    MATCH,
    MISMATCH,
    compare,
    derive_tokens,
)
from axiom.modules.replay.packet import (
    PacketError,
    parse_request,
    parse_response,
)
from axiom.modules.replay.render import (
    RenderError,
    RenderInput,
    build_html,
    resolve_chromium,
    screenshot,
)
from axiom.modules.replay.sender import (
    ReplayBlocked,
    SendResult,
    assert_public_target,
    scope_allows,
    send,
)


PACKET = (
    "POST /api/order/list HTTP/1.1\n"
    "Host: api.example.com\n"
    "Cookie: session=abc123\n"
    "Content-Type: application/json\n"
    "\n"
    '{"page":1,"size":20}'
)


# ── packet parsing ───────────────────────────────────────────────────────────


def test_parse_request_keeps_every_credential_header() -> None:
    """A replay that drops Cookie would silently verify an unauthenticated view."""
    packet = parse_request(PACKET)
    assert packet.method == "POST"
    assert packet.target == "/api/order/list"
    assert packet.host == "api.example.com"
    assert packet.headers["Cookie"] == "session=abc123"
    assert packet.body == '{"page":1,"size":20}'
    assert packet.url_for("https") == "https://api.example.com/api/order/list"


def test_parse_request_rejects_curl_instead_of_guessing() -> None:
    with pytest.raises(PacketError) as excinfo:
        parse_request("curl -X POST https://a.example.com/api -d '{}'")
    assert "报文" in str(excinfo.value)


def test_parse_request_requires_a_host() -> None:
    with pytest.raises(PacketError) as excinfo:
        parse_request("GET /api/x HTTP/1.1\nAccept: */*\n\n")
    assert "Host" in str(excinfo.value)


def test_parse_request_accepts_absolute_form_target() -> None:
    packet = parse_request(
        "GET https://api.example.com:8443/x?y=1 HTTP/1.1\nHost: ignored.example\n\n"
    )
    assert packet.host == "api.example.com"
    assert packet.url_for("http") == "https://api.example.com:8443/x?y=1"


def test_parse_request_round_trips_for_display() -> None:
    packet = parse_request(PACKET)
    assert packet.to_text() == PACKET


def test_parse_response_reads_status_and_content_type() -> None:
    response = parse_response(
        "HTTP/1.1 200 OK\nContent-Type: application/json; charset=utf-8\n\n{\"code\":0}"
    )
    assert response.status_code == 200
    assert response.content_type() == "application/json"
    assert response.status_line == "HTTP/1.1 200 OK"


# ── private-network refusal ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.0.0.5", "192.168.0.95", "172.16.4.4", "169.254.1.1", "::1"],
)
def test_private_targets_are_refused_before_sending(address, monkeypatch) -> None:
    monkeypatch.setattr(
        "axiom.modules.replay.sender.resolve_addresses", lambda host, port: [address]
    )
    with pytest.raises(ReplayBlocked) as excinfo:
        assert_public_target("internal.example.com", 443)
    assert "内网" in excinfo.value.reason
    assert "webshell" in excinfo.value.hint


def test_one_private_answer_among_public_ones_still_blocks(monkeypatch) -> None:
    """A hostname with both kinds of record is a way into the network, not a way out."""
    monkeypatch.setattr(
        "axiom.modules.replay.sender.resolve_addresses",
        lambda host, port: ["93.184.216.34", "192.168.0.95"],
    )
    with pytest.raises(ReplayBlocked):
        assert_public_target("mixed.example.com", 443)


def test_public_target_passes(monkeypatch) -> None:
    monkeypatch.setattr(
        "axiom.modules.replay.sender.resolve_addresses", lambda host, port: ["93.184.216.34"]
    )
    assert assert_public_target("example.com", 443) == ["93.184.216.34"]


def test_allow_private_is_an_explicit_escape_hatch(monkeypatch) -> None:
    """Only tests and the CLI flag use this; it must never be the default."""
    monkeypatch.setattr(
        "axiom.modules.replay.sender.resolve_addresses", lambda host, port: ["127.0.0.1"]
    )
    assert assert_public_target("localhost", 80, allow_private=True) == []


# ── scope ───────────────────────────────────────────────────────────────────


def test_scope_allows_an_in_range_host() -> None:
    scope = {"enabled": True, "targets": ["example.com"], "exclude": []}
    allowed, _ = scope_allows(scope, "api.example.com")
    assert allowed is True


def test_scope_exclude_wins_over_targets() -> None:
    scope = {"enabled": True, "targets": ["example.com"], "exclude": ["admin.example.com"]}
    allowed, why = scope_allows(scope, "admin.example.com")
    assert allowed is False
    assert "排除" in why


def test_scope_rejects_a_host_outside_the_engagement() -> None:
    scope = {"enabled": True, "targets": ["example.com"], "exclude": []}
    allowed, why = scope_allows(scope, "other.test")
    assert allowed is False
    assert "scope" in why


def test_scope_without_targets_is_open() -> None:
    assert scope_allows({"enabled": True, "targets": [], "exclude": []}, "anything.test")[0] is True


# ── sending: refusals happen before any socket work ─────────────────────────


def test_delete_is_refused_without_the_flag(monkeypatch) -> None:
    monkeypatch.setattr(
        "axiom.modules.replay.sender.resolve_addresses", lambda host, port: ["93.184.216.34"]
    )
    packet = parse_request("DELETE /api/x HTTP/1.1\nHost: example.com\n\n")
    with pytest.raises(ReplayBlocked) as excinfo:
        send(packet)
    assert "DELETE" in excinfo.value.reason


def test_network_failure_is_reported_not_raised(monkeypatch) -> None:
    """A dead target must not crash the review script; it becomes a finding."""
    monkeypatch.setattr(
        "axiom.modules.replay.sender.resolve_addresses", lambda host, port: ["93.184.216.34"]
    )

    import httpx

    class Boom(httpx.Client):
        def __init__(self, *a, **kw):
            raise httpx.ConnectError("nope")

    monkeypatch.setattr("axiom.modules.replay.sender.httpx.Client", Boom)
    result = send(parse_request(PACKET))
    assert result.sent is False
    assert result.error_kind in {"connect", "tls", "unreachable"}


# ── comparison ──────────────────────────────────────────────────────────────


def _live(**overrides) -> SendResult:
    base = dict(
        sent=True,
        method="POST",
        url="https://api.example.com/api/order/list",
        status_code=200,
        reason="OK",
        headers={"content-type": "application/json"},
        body='{"code":0,"data":[{"order_id":8801}]}',
        elapsed_ms=42,
    )
    base.update(overrides)
    return SendResult(**base)


def test_compare_matches_when_status_and_fragments_agree() -> None:
    result = compare(
        '{"code":0,"data":[{"order_id":8801}]}',
        200,
        "application/json",
        _live(),
    )
    assert result.status == MATCH
    assert result.failed == 0


def test_compare_flags_a_different_status() -> None:
    result = compare('{"code":0}', 200, "application/json", _live(status_code=403))
    assert result.status == MISMATCH
    assert any(check.name == "状态码" and check.ok is False for check in result.checks)


def test_compare_flags_a_missing_fragment() -> None:
    result = compare(
        '{"code":0,"data":[{"order_id":8801}]}',
        200,
        "application/json",
        _live(body='{"code":0,"data":[]}'),
    )
    assert result.status == MISMATCH
    assert any(check.name == "关键片段" and check.ok is False for check in result.checks)


def test_compare_is_inconclusive_when_nothing_can_be_compared() -> None:
    """Unreviewable is not the same as wrong; the reviewer must see the difference."""
    result = compare("", None, "", _live())
    assert result.status == INCONCLUSIVE


def test_compare_is_inconclusive_when_the_send_failed() -> None:
    result = compare('{"code":0}', 200, "application/json", _live(sent=False, status_code=None, error_kind="timeout"))
    assert result.status == INCONCLUSIVE
    assert any("未能发送" in note for note in result.notes)


def test_extra_asserts_are_honoured() -> None:
    result = compare("", None, "", _live(body='{"tenant":"acme"}'), extra_asserts=["acme"])
    assert result.status == MATCH
    assert any(check.expected == "acme" for check in result.checks)


def test_derive_tokens_skips_noise_values() -> None:
    """"code":0 matches almost any API; it must not be the only evidence of a match."""
    tokens = derive_tokens('{"code":0,"ok":true,"order_id":8801}')
    assert all(token not in {"code", "ok"} for token in tokens)
    assert any("8801" in token for token in tokens)


# ── rendering ───────────────────────────────────────────────────────────────


def test_html_shows_both_sides_and_escapes_markup() -> None:
    document = build_html(
        RenderInput(
            packet_text="POST /x HTTP/1.1\nHost: a.example.com\n\n<script>alert(1)</script>",
            response_text="HTTP/1.1 200 OK\n\n<b>ok</b>",
            title="标题",
            highlights=['"code":0'],
        )
    )
    assert "请求" in document and "响应" in document
    # A packet that contains HTML must be shown as text, never executed.
    assert "<script>alert(1)</script>" not in document
    assert "&lt;script&gt;" in document


def test_html_highlights_the_requested_fragment() -> None:
    document = build_html(
        RenderInput(
            packet_text="GET /x HTTP/1.1\nHost: a.example.com\n\n",
            response_text='HTTP/1.1 200 OK\n\n{"code":0}',
            highlights=['"code":0'],
        )
    )
    assert "<mark>" in document


def test_html_states_truncation_instead_of_hiding_it() -> None:
    document = build_html(
        RenderInput(
            packet_text="GET /x HTTP/1.1\nHost: a.example.com\n\n",
            response_text="HTTP/1.1 200 OK\n\n{}",
            body_truncated=True,
            original_body_bytes=900_000,
        )
    )
    assert "已截断" in document


@pytest.mark.skipif(resolve_chromium() is None, reason="需要本机 chromium 才能验证渲染")
def test_screenshot_produces_a_non_blank_png_of_the_expected_width() -> None:
    png = screenshot(
        RenderInput(
            packet_text=PACKET,
            response_text='HTTP/1.1 200 OK\nContent-Type: application/json\n\n{"code":0}',
            title="t",
            highlights=['"code":0'],
        )
    )
    assert png.startswith(b"\x89PNG")
    # 1440 CSS px at 2x.  Width is what the report template assumes.
    width = int.from_bytes(png[16:20], "big")
    assert width == 2880
    height = int.from_bytes(png[20:24], "big")
    assert height > 100


@pytest.mark.skipif(resolve_chromium() is None, reason="需要本机 chromium 才能验证渲染")
def test_screenshot_height_follows_content() -> None:
    """A short exchange must not leave the tall empty band a fixed viewport produced."""
    short = screenshot(
        RenderInput(
            packet_text="GET /x HTTP/1.1\nHost: a.example.com\n\n",
            response_text="HTTP/1.1 200 OK\n\n{}",
        )
    )
    tall = screenshot(
        RenderInput(
            packet_text="GET /x HTTP/1.1\nHost: a.example.com\n\n",
            response_text="HTTP/1.1 200 OK\n\n" + ("line\n" * 40),
        )
    )
    short_height = int.from_bytes(short[20:24], "big")
    tall_height = int.from_bytes(tall[20:24], "big")
    assert tall_height > short_height


def test_render_failure_is_typed_so_the_send_result_survives() -> None:
    with pytest.raises(RenderError):
        screenshot(RenderInput(packet_text="", response_text=""), chromium="/nonexistent/chrome")


# ── end to end through the service layer ────────────────────────────────────


def _reported_result(client, engine, *, request_packet: str, response_packet: str) -> tuple[str, str]:
    """Create a red-team task + one reported result carrying a reproduction.

    Rows are inserted directly because the real reporting path needs a live Explore
    lease, which is unrelated machinery here; the columns mirror the table exactly so
    a schema change fails the test instead of silently inserting a partial row.
    """
    from sqlalchemy import insert

    from axiom.modules.graph.tables import intents
    from axiom.modules.redteam.tables import red_team_chains, red_team_results
    from axiom.modules.reproduction.schemas import Reproduction, ReproStep
    from axiom.shared.clock import utcnow

    project = client.post("/api/v1/projects", json={"title": "重放"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "重放任务",
            "origin": "a.example.com",
            "worker_pool": ["worker-a"],
            "engagement_mode": "red_team",
        },
    ).json()
    assert "id" in task, f"task creation failed: {task}"
    task_id = task["id"]
    now = utcnow()
    reproduction = Reproduction(
        entry_point="POST /api/order/list",
        steps=[
            ReproStep(
                title="越权读取他人订单",
                request=request_packet,
                response=response_packet,
                proves="拿到了不属于本账号的订单",
            )
        ],
    )
    with engine.begin() as conn:
        conn.execute(
            insert(intents).values(
                id="i002",
                task_id=task_id,
                chain_id="ch001",
                kind="explore",
                description="越权读取订单",
                status="completed",
                creator="test",
                created_at=now,
            )
        )
        conn.execute(
            insert(red_team_chains).values(
                id="ch001",
                task_id=task_id,
                title="链",
                initial_principal_json={
                    "id": "user:initial", "label": "初始身份", "tenant": "", "credential_id": None
                },
                status="open",
                created_by="test",
                created_at=now,
                updated_at=now,
            )
        )
        conn.execute(
            insert(red_team_results).values(
                id="x001",
                task_id=task_id,
                chain_id="ch001",
                intent_id="i002",
                result_type="data",
                title="越权读取订单列表",
                description="读取了他人订单",
                principal="普通成员",
                target_asset="a.example.com",
                sample_count=1,
                sample_bytes=100,
                data_fields_json='["order_id"]',
                reproduction_json=reproduction.model_dump_json(),
                status="reported",
                reported_by="explore",
                points_declared=10,
                points_awarded=0,
                created_at=now,
                updated_at=now,
            )
        )
    return task_id, "x001"


def test_replay_service_sends_and_compares_against_the_recorded_response(client, engine, replay_target) -> None:
    """The live answer is compared to the recorded one, not to a caller assertion."""

    from axiom.modules.replay.service import ReplayService

    port, _ = replay_target
    task_id, result_id = _reported_result(
        client,
        engine,
        request_packet=f"GET / HTTP/1.1\nHost: 127.0.0.1:{port}\n\n",
        response_packet='HTTP/1.1 999 Recorded\nContent-Type: text/plain\n\nnope',
    )
    container = client.app.state.container
    service = ReplayService(
        engine,
        tasks=container.tasks,
        red_team=container.red_team,
        evidence=container.report_evidence,
    )

    # 127.0.0.1 is refused by default; the flag is what makes this test local-only.
    outcome = service.replay(
        task_id=task_id,
        result_id=result_id,
        step=1,
        allow_private=True,
        capture=False,
    )
    assert outcome.sent is True
    assert outcome.request_text.startswith("GET / HTTP/1.1")
    # The recorded status was fabricated, so the live answer cannot match it.
    assert outcome.comparison.status in {"mismatch", "inconclusive"}


def test_replay_service_refuses_private_targets_by_default(client, engine) -> None:
    from axiom.modules.replay.service import ReplayError, ReplayService

    task_id, result_id = _reported_result(
        client,
        engine,
        request_packet="GET / HTTP/1.1\nHost: 127.0.0.1\n\n",
        response_packet="HTTP/1.1 200 OK\n\n{}",
    )
    container = client.app.state.container
    service = ReplayService(
        engine,
        tasks=container.tasks,
        red_team=container.red_team,
        evidence=container.report_evidence,
    )
    with pytest.raises(ReplayError) as excinfo:
        service.replay(task_id=task_id, result_id=result_id, step=1, capture=False)
    assert excinfo.value.status == 403
    assert "内网" in excinfo.value.message


def test_replay_service_rejects_an_out_of_range_step(client, engine) -> None:
    from axiom.modules.replay.service import ReplayError, ReplayService

    task_id, result_id = _reported_result(
        client,
        engine,
        request_packet="GET / HTTP/1.1\nHost: 127.0.0.1\n\n",
        response_packet="HTTP/1.1 200 OK\n\n{}",
    )
    container = client.app.state.container
    service = ReplayService(
        engine,
        tasks=container.tasks,
        red_team=container.red_team,
        evidence=container.report_evidence,
    )
    with pytest.raises(ReplayError) as excinfo:
        service.replay(task_id=task_id, result_id=result_id, step=5, capture=False)
    assert "越界" in excinfo.value.message


def test_replay_ignores_a_caller_supplied_packet_for_a_reported_result(client, engine) -> None:
    """A screenshot must show the step that was reported, not text a caller typed."""

    from axiom.modules.replay.service import ReplayService

    task_id, result_id = _reported_result(
        client,
        engine,
        request_packet="GET /recorded HTTP/1.1\nHost: 127.0.0.1\n\n",
        response_packet="HTTP/1.1 200 OK\n\n{}",
    )
    container = client.app.state.container
    service = ReplayService(
        engine,
        tasks=container.tasks,
        red_team=container.red_team,
        evidence=container.report_evidence,
    )
    outcome = service.replay(
        task_id=task_id,
        result_id=result_id,
        step=1,
        packet_text="GET /tampered HTTP/1.1\nHost: 127.0.0.1\n\n",
        allow_private=True,
        capture=False,
    )
    assert "/recorded" in outcome.request_text
    assert "/tampered" not in outcome.request_text


@pytest.fixture()
def replay_target():
    """A real HTTP server on loopback, so tests exercise an actual send."""
    import http.server
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib naming
            body = b'{"code":0,"order_id":8801}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # silence the test run
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], "http://127.0.0.1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_replay_service_reports_a_match_when_the_target_agrees(client, engine, replay_target) -> None:
    """The common case: the target still answers as reported, so the step is verified."""
    from axiom.modules.replay.service import ReplayService

    port, _ = replay_target
    recorded_body = '{"code":0,"order_id":8801}'
    task_id, result_id = _reported_result(
        client,
        engine,
        request_packet=f"GET / HTTP/1.1\nHost: 127.0.0.1:{port}\n\n",
        response_packet=(
            "HTTP/1.1 200 OK\n"
            "Content-Type: application/json\n\n"
            + recorded_body
        ),
    )
    container = client.app.state.container
    service = ReplayService(
        engine,
        tasks=container.tasks,
        red_team=container.red_team,
        evidence=container.report_evidence,
    )
    outcome = service.replay(
        task_id=task_id, result_id=result_id, step=1, allow_private=True, capture=False
    )
    assert outcome.sent is True
    assert outcome.live.status_code == 200
    assert outcome.comparison.status == "match"


@pytest.mark.skipif(resolve_chromium() is None, reason="需要本机 chromium 才能验证截图")
def test_shot_attaches_a_packet_evidence_row_to_the_step(client, engine, replay_target) -> None:
    """The whole point: one call proves the step and files the picture that says so.

    ``kind`` must be ``packet`` — the review gate counts only packet shots, so a
    business-kind row here would let the gate pass on a picture that is not evidence.
    """
    from axiom.modules.replay.service import ReplayService

    port, _ = replay_target
    task_id, result_id = _reported_result(
        client,
        engine,
        request_packet=f"GET / HTTP/1.1\nHost: 127.0.0.1:{port}\n\n",
        response_packet=(
            "HTTP/1.1 200 OK\nContent-Type: application/json\n\n"
            '{"code":0,"order_id":8801}'
        ),
    )
    container = client.app.state.container
    service = ReplayService(
        engine,
        tasks=container.tasks,
        red_team=container.red_team,
        evidence=container.report_evidence,
    )
    outcome = service.replay(
        task_id=task_id,
        result_id=result_id,
        step=1,
        allow_private=True,
        capture=True,
        attach=True,
        caption="越权读取他人订单",
    )

    assert outcome.sent is True
    assert outcome.comparison.status == "match"
    assert outcome.artifact_id, "截图未上传"
    assert outcome.screenshot_error == ""

    rows = container.report_evidence.list(task_id, "redteam_result", result_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["artifact_id"] == outcome.artifact_id
    assert row["step_index"] == 1
    assert row["kind"] == "packet"
    assert row["caption"] == "越权读取他人订单"


@pytest.mark.skipif(resolve_chromium() is None, reason="需要本机 chromium 才能验证截图")
def test_a_covered_step_closes_the_review_gate(client, engine, replay_target) -> None:
    """After a replay attaches a shot, the gate that blocked the verdict opens."""
    from axiom.modules.replay.service import ReplayService
    from axiom.modules.reports.evidence import missing_packet_steps

    port, _ = replay_target
    task_id, result_id = _reported_result(
        client,
        engine,
        request_packet=f"GET / HTTP/1.1\nHost: 127.0.0.1:{port}\n\n",
        response_packet="HTTP/1.1 200 OK\n\n{}",
    )
    container = client.app.state.container
    service = ReplayService(
        engine,
        tasks=container.tasks,
        red_team=container.red_team,
        evidence=container.report_evidence,
    )

    before = missing_packet_steps(
        1, container.report_evidence.list(task_id, "redteam_result", result_id)
    )
    assert before == [1], "未重放前，闸门应认为第 1 步缺报文截图"

    service.replay(
        task_id=task_id, result_id=result_id, step=1,
        allow_private=True, capture=True, attach=True,
    )

    after = missing_packet_steps(
        1, container.report_evidence.list(task_id, "redteam_result", result_id)
    )
    assert after == []


# ── 一次性渲染页的数据存储 ──────────────────────────────────────────────────


def test_store_expires_entries_after_the_ttl() -> None:
    """数据只活到截图那一刻；留久了等于把会话 cookie 和响应体挂在内存里。"""
    from axiom.modules.replay.store import ExchangeStore

    store = ExchangeStore(ttl_seconds=10)
    entry = store.put(request_text="GET / HTTP/1.1\nHost: a.test\n\n", response_text="HTTP/1.1 200 OK\n\n")
    assert store.get(entry.key) is not None
    # 把创建时间往前拨，模拟过期
    entry.created_at -= 11
    assert store.get(entry.key) is None


def test_store_bounds_itself_by_dropping_the_oldest() -> None:
    from axiom.modules.replay.store import ExchangeStore

    store = ExchangeStore(ttl_seconds=600, max_entries=3)
    keys = [
        store.put(request_text=f"GET /{i} HTTP/1.1\nHost: a.test\n\n", response_text="HTTP/1.1 200 OK\n\n").key
        for i in range(5)
    ]
    assert store.stats()["entries"] == 3
    assert store.get(keys[0]) is None        # 最早的被挤掉
    assert store.get(keys[-1]) is not None   # 最新的还在


def test_store_keys_are_unguessable() -> None:
    from axiom.modules.replay.store import ExchangeStore

    store = ExchangeStore()
    first = store.put(request_text="GET / HTTP/1.1\nHost: a.test\n\n", response_text="HTTP/1.1 200 OK\n\n")
    second = store.put(request_text="GET / HTTP/1.1\nHost: a.test\n\n", response_text="HTTP/1.1 200 OK\n\n")
    assert first.key != second.key
    assert len(first.key) >= 20


def test_expired_page_is_gone_not_blank() -> None:
    """过期要有明确说法，否则审核者以为是渲染坏了。"""
    from axiom.modules.replay.web import _EXPIRED_HTML

    assert "过期" in _EXPIRED_HTML
    assert "10 分钟" in _EXPIRED_HTML
