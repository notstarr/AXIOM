"""Curl input: translated, not rejected, and never silently lossy.

Workers paste curl because that is what they typed at the shell.  Accepting it removes a
class of mistakes (a hand-expanded packet that drops a header), but only if the
translation is faithful.  These tests pin the two properties that matter: the common
flags land in the packet exactly, and anything that would change what goes on the wire
raises with the flag named instead of being ignored.
"""

from __future__ import annotations

import pytest

from axiom.modules.reproduction.curl import (
    CurlParseError,
    curl_to_packet,
    is_curl_command,
    parse_curl,
)


def test_detects_a_pasted_command_only() -> None:
    assert is_curl_command("curl https://a.test/x") is True
    assert is_curl_command("  $ curl https://a.test/x") is True
    assert is_curl_command("sudo curl https://a.test/x") is True
    assert is_curl_command("GET /x HTTP/1.1\nHost: a.test\n\n") is False
    assert is_curl_command("wget https://a.test/x") is False


def test_method_url_headers_and_body_survive() -> None:
    packet = curl_to_packet(
        "curl -X POST 'https://api.test/order?x=1' "
        "-H 'Cookie: session=abc' -H 'X-Trace: 9' -d '{\"page\":1}'"
    )
    assert packet.startswith("POST /order?x=1 HTTP/1.1\n")
    assert "Host: api.test\n" in packet
    assert "Cookie: session=abc\n" in packet
    assert "X-Trace: 9\n" in packet
    # -d implies the form content type, which is what curl actually sends.
    assert "Content-Type: application/x-www-form-urlencoded" in packet
    assert packet.endswith("\n\n{\"page\":1}")


def test_short_flags_are_expanded_into_headers() -> None:
    """``-b``/``-A`` are how curl carries credentials and identity; dropping them
    would make the replayed request anonymous."""
    packet = curl_to_packet('curl https://a.test/api -b "s=1" -A "Mozilla/5.0"')
    assert "Cookie: s=1" in packet
    assert "User-Agent: Mozilla/5.0" in packet


def test_basic_auth_becomes_an_authorization_header() -> None:
    packet = curl_to_packet("curl -u admin:pass https://a.test/login")
    assert "Authorization: Basic YWRtaW46cGFzcw==" in packet


def test_json_flag_sets_the_json_content_type() -> None:
    packet = curl_to_packet("curl --json '{\"a\":1}' https://a.test/x")
    assert packet.startswith("POST /x HTTP/1.1")
    assert "Content-Type: application/json" in packet


def test_post_is_inferred_from_data() -> None:
    assert curl_to_packet("curl https://a.test/x -d 'a=1'").startswith("POST /x")
    assert curl_to_packet("curl https://a.test/x").startswith("GET /x")
    assert curl_to_packet("curl -I https://a.test/x").startswith("HEAD /x")


def test_client_only_flags_do_not_change_the_packet() -> None:
    """``-k``/``-L``/``-s`` shape curl's behaviour, not the request on the wire."""
    with_flags = curl_to_packet("curl -sS -k -L --compressed https://a.test/x")
    plain = curl_to_packet("curl https://a.test/x")
    assert with_flags == plain


def test_multiline_paste_with_continuations() -> None:
    command = """curl -X POST https://a.test/upload \\
      -H 'Content-Type: application/json' \\
      -d '{"a":1}'"""
    packet = curl_to_packet(command)
    assert "POST /upload" in packet
    assert '{"a":1}' in packet


@pytest.mark.parametrize(
    "flag,command",
    [
        ("--cert", "curl --cert client.pem https://a.test/x"),
        ("--proxy", "curl --proxy http://127.0.0.1:8080 https://a.test/x"),
        ("-T", "curl -T /tmp/f https://a.test/x"),
        ("--resolve", "curl --resolve a.test:443:1.2.3.4 https://a.test/x"),
    ],
)
def test_flags_that_would_lie_about_the_wire_are_refused(flag: str, command: str) -> None:
    """A packet that silently omitted these would describe a different request."""
    with pytest.raises(CurlParseError) as excinfo:
        parse_curl(command)
    assert flag in str(excinfo.value)


def test_unknown_flags_are_refused_rather_than_ignored() -> None:
    with pytest.raises(CurlParseError) as excinfo:
        parse_curl("curl --frobnicate https://a.test/x")
    assert "--frobnicate" in str(excinfo.value)


def test_two_urls_are_refused() -> None:
    with pytest.raises(CurlParseError) as excinfo:
        parse_curl("curl https://a.test/one https://a.test/two")
    assert "多个 URL" in str(excinfo.value)


def test_missing_url_is_refused() -> None:
    with pytest.raises(CurlParseError):
        parse_curl("curl -X POST")


def test_host_header_override_wins_over_the_url() -> None:
    """Testing host-header routing is a real technique; the packet must say so."""
    packet = curl_to_packet(
        "curl https://a.test/x -H 'Host: internal.test'"
    )
    assert "Host: internal.test" in packet
    assert "Host: a.test" not in packet


def test_unterminated_quote_is_reported_as_such() -> None:
    with pytest.raises(CurlParseError) as excinfo:
        parse_curl("curl https://a.test/x -d 'unclosed")
    assert "引号" in str(excinfo.value)


# ── integration with the schema that stores it ──────────────────────────────


def test_schema_stores_the_translated_packet_not_the_command() -> None:
    from axiom.modules.reproduction.schemas import ReproExchange

    exchange = ReproExchange(
        request="curl -X POST 'https://a.test/x' -H 'Cookie: s=1' -d '{}'",
        response="HTTP/1.1 200 OK\n\n{}",
    )
    assert exchange.request.startswith("POST /x HTTP/1.1")
    assert "curl" not in exchange.request
    assert "Cookie: s=1" in exchange.request


def test_schema_rejects_a_command_that_cannot_be_translated() -> None:
    from axiom.modules.reproduction.schemas import ReproExchange

    with pytest.raises(ValueError) as excinfo:
        ReproExchange(
            request="curl --cert x.pem https://a.test/x",
            response="HTTP/1.1 200 OK\n\n{}",
        )
    assert "curl" in str(excinfo.value)


def test_a_real_packet_still_passes_through_untouched() -> None:
    from axiom.modules.reproduction.schemas import ReproExchange

    packet = "GET /x HTTP/1.1\nHost: a.test\nCookie: s=1\n\n"
    assert ReproExchange(request=packet, response="HTTP/1.1 200 OK\n\n").request == packet.strip()


# ── 复现必填的开关 ──────────────────────────────────────────────────────────


def _vuln_payload(**overrides):
    base = {
        "title": "越权读取订单",
        "type": "越权",
        "severity": "medium",
        "description": "普通用户可读他人订单",
        "impact": "泄露用户订单数据",
    }
    base.update(overrides)
    return base


def test_vulnerability_reproduction_is_required_by_default(monkeypatch) -> None:
    """没有可复现路径的漏洞无法被审核，也无法生成报文截图。"""
    from axiom.modules.vulnerabilities.schemas import VulnerabilityReport, reproduction_required

    monkeypatch.delenv("AXIOM_REQUIRE_REPRODUCTION", raising=False)
    assert reproduction_required() is True
    with pytest.raises(ValueError) as excinfo:
        VulnerabilityReport(**_vuln_payload())
    assert "复现过程" in str(excinfo.value)
    # The message has to say how, not just refuse — curl is the cheap path.
    assert "curl" in str(excinfo.value)


def test_the_switch_can_reopen_manual_entry(monkeypatch) -> None:
    """过渡期可以不改代码就放开人工无报文录入。"""
    from axiom.modules.vulnerabilities.schemas import VulnerabilityReport, reproduction_required

    monkeypatch.setenv("AXIOM_REQUIRE_REPRODUCTION", "0")
    assert reproduction_required() is False
    report = VulnerabilityReport(**_vuln_payload())
    assert report.reproduction is None


def test_with_the_switch_on_a_reproduction_satisfies_it(monkeypatch) -> None:
    from axiom.modules.vulnerabilities.schemas import VulnerabilityReport

    monkeypatch.setenv("AXIOM_REQUIRE_REPRODUCTION", "1")
    report = VulnerabilityReport(
        **_vuln_payload(
            reproduction={
                "entry_point": "GET /api/order",
                "steps": [
                    {
                        "title": "读取他人订单",
                        "request": "curl https://a.test/api/order -H 'Cookie: s=1'",
                        "response": "HTTP/1.1 200 OK\n\n{\"ok\":1}",
                        "proves": "越权成立",
                    }
                ],
            }
        )
    )
    # The curl became a packet on the way in.
    assert report.reproduction.steps[0].exchanges[0].request.startswith("GET /api/order HTTP/1.1")
