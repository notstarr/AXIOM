"""The asset/interface index: what the task touched, derived from real packets only.

The index is the one chapter of the task report that makes a factual claim the reader can
check, so its rules are pinned here:

* one interface is one row — ``/products/363`` and ``/products/712`` are the same row;
* a packet with no ``Host:`` is counted, not dropped, and appears under its own heading;
* ``entry_point`` is never parsed.  It is prose (``"(SQLite UNION 注入) → POST /x``), and
  parsing it silently invents interfaces that were never called.
"""

from __future__ import annotations

from types import SimpleNamespace

from axiom.modules.reports.interfaces import build_index, normalise_path, parse_exchange


def _packet(method: str, path: str, host: str = "a.example.com") -> str:
    return f"{method} {path} HTTP/1.1\r\nHost: {host}\r\nCookie: sid=x\r\n\r\n"


def _reply(status: int = 200) -> str:
    return f"HTTP/1.1 {status} OK\r\nContent-Type: application/json\r\n\r\n{{}}"


def _finding(finding_id: str, *exchanges: tuple[str, str], **overrides):
    """A minimal Vulnerability stand-in: the index only reads id/severity/reproduction."""

    steps = [
        SimpleNamespace(
            title="step",
            proves="proof",
            exchanges=[
                SimpleNamespace(request=request, response=response)
                for request, response in exchanges
            ],
        )
    ]
    return SimpleNamespace(
        id=finding_id,
        severity=overrides.get("severity", "high"),
        reproduction=SimpleNamespace(steps=steps),
    )


def test_numeric_and_uuid_segments_fold_into_one_interface() -> None:
    assert normalise_path("/api/mall/products/363/reviews") == "/api/mall/products/{id}/reviews"
    assert normalise_path("/api/mall/products/712/reviews") == "/api/mall/products/{id}/reviews"
    assert (
        normalise_path("/u/0b3072ed-2aec-4534-8756-9747f72a26ea/x")
        == "/u/{uuid}/x"
    )
    # Query strings are input, not identity: two SQLi payloads are one interface.
    assert normalise_path("/api/mall/products?q=1") == "/api/mall/products"
    assert normalise_path("/api/mall/products?q=zzz%27+OR+1%3D1") == "/api/mall/products"


def test_a_parse_requires_a_request_line() -> None:
    # Commands and truncated pastes must not become phantom rows.
    assert parse_exchange("curl https://a.example.com/x", "") is None
    assert parse_exchange("", "") is None
    parsed = parse_exchange(_packet("GET", "/x?y=1"), "HTTP/1.1 404 Not Found\r\n")
    assert parsed is not None
    assert (parsed.method, parsed.host, parsed.path, parsed.status) == ("GET", "a.example.com", "/x", 404)
    assert parsed.raw_path == "/x?y=1"


def test_an_absolute_form_target_supplies_the_host_itself() -> None:
    """A request captured through a proxy writes the authority in the request line."""
    parsed = parse_exchange("GET https://b.example.com/api/z HTTP/1.1\r\n\r\n", "")
    assert parsed is not None
    assert (parsed.host, parsed.path) == ("b.example.com", "/api/z")


def test_userinfo_never_reaches_the_reported_host() -> None:
    parsed = parse_exchange("GET http://user:pass@c.example.com/p HTTP/1.1\r\n\r\n", "")
    assert parsed is not None
    assert parsed.host == "c.example.com"


def test_host_is_lowercased_and_loses_its_port() -> None:
    parsed = parse_exchange(_packet("GET", "/p", host="A.Example.COM:8443"), "")
    assert parsed is not None
    assert parsed.host == "a.example.com"


def test_one_interface_counts_every_call_and_every_finding() -> None:
    index = build_index(
        [
            _finding(
                "v001",
                (_packet("GET", "/api/x/1"), _reply(200)),
                (_packet("GET", "/api/x/2"), _reply(200)),
            ),
            _finding("v002", (_packet("GET", "/api/x/3"), _reply(403))),
        ]
    )
    assert [row.host for row in index.hosts] == ["a.example.com"]
    [interface] = index.interfaces
    assert interface.path == "/api/x/{id}"
    assert interface.calls == 3
    assert interface.finding_ids == {"v001", "v002"}
    assert interface.status_text == "200/403"
    # The concrete form survives so a reader can replay something real.
    assert interface.example_path == "/api/x/1"


def test_a_packet_without_a_host_is_kept_and_reported_separately() -> None:
    index = build_index(
        [
            _finding(
                "v001",
                (_packet("GET", "/api/ok"), ""),
                ("GET /api/orphan HTTP/1.1\r\n\r\n", ""),
                ("garbage not a request", ""),
            )
        ]
    )
    assert len(index.interfaces) == 1
    assert len(index.unattributed) == 1
    assert index.unattributed[0].path == "/api/orphan"
    assert index.unattributed[0].host == ""
    # Interface count includes both, so the summary cannot understate the surface.
    assert index.interface_count == 2


def test_hosts_are_ordered_by_traffic_and_carry_the_worst_severity() -> None:
    quiet = _finding("v001", (_packet("GET", "/q", host="quiet.example.com"), ""), severity="low")
    busy_low = _finding(
        "v002", (_packet("GET", "/b", host="busy.example.com"), ""), severity="low"
    )
    busy_high = _finding(
        "v003",
        (
            _packet("GET", "/b", host="busy.example.com"),
            _packet("GET", "/b2", host="busy.example.com"),
        ),
        severity="critical",
    )
    index = build_index([quiet, busy_low, busy_high])
    assert [row.host for row in index.hosts] == ["busy.example.com", "quiet.example.com"]
    assert index.hosts[0].worst_severity == "critical"
    assert index.hosts[1].worst_severity == "low"
    assert index.hosts[0].first_finding_id == "v002"


def test_a_finding_without_reproduction_contributes_nothing_but_breaks_nothing() -> None:
    bare = SimpleNamespace(id="v009", severity="high", reproduction=None)
    index = build_index([bare])
    assert index.host_count == 0
    assert index.interface_count == 0
    assert index.call_count == 0


def test_interfaces_for_one_finding_are_scoped_to_it() -> None:
    first = _finding("v001", (_packet("GET", "/api/a"), ""))
    second = _finding("v002", (_packet("GET", "/api/b"), ""))
    index = build_index([first, second])
    assert [row.path for row in index.interfaces_for("v001")] == ["/api/a"]
    assert [row.path for row in index.interfaces_for("v002")] == ["/api/b"]
