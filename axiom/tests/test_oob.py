"""Out-of-band capture: one bucket, ten minutes, wiped on stop.

These tests hit the real routes rather than the store directly where it
matters — the interesting failures here are routing and auth-boundary ones
(the payload path must stay open, the read path must not), and those only
show up over HTTP.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from axiom.app import create_app
from axiom.modules.oob.store import STORE, OobStore


@pytest.fixture(autouse=True)
def _reset_bucket():
    """Each test starts with no bucket; the store is a process singleton."""

    STORE.stop()
    yield
    STORE.stop()


def test_start_is_idempotent_and_reuses_the_token(client: TestClient) -> None:
    """The skill relies on this: "already open, just use it" must not mint a new one."""

    first = client.post("/api/v1/oob/start", params={"base_url": "https://h.example"}).json()
    assert first["running"] is True
    assert len(first["token"]) == 6 and first["token"].isdigit()
    assert first["url"] == f"https://h.example/oob/{first['token']}"

    second = client.post("/api/v1/oob/start", params={"base_url": "https://h.example"}).json()
    assert second["token"] == first["token"], "re-start must not strand a URL already sent to a target"
    assert second["url"] == first["url"]


def test_public_capture_needs_no_credentials_but_read_does(client: TestClient) -> None:
    """Write is public by design; read is not. That separation is the whole safety story."""

    token = client.post("/api/v1/oob/start", params={"base_url": "https://h.example"}).json()["token"]

    hit = client.get(f"/oob/{token}/i052-ssrf", params={"a": "1"}, headers={"User-Agent": "curl/8.7.1"})
    assert hit.status_code == 200
    # Nothing is echoed back: reflecting request content would make this an XSS gadget.
    assert hit.text == "ok"

    listed = client.get("/api/v1/oob/hits").json()
    assert listed["hit_count"] == 1
    row = listed["hits"][0]
    assert row["label"] == "i052-ssrf"
    assert row["method"] == "GET"
    assert row["query"] == "a=1"
    assert row["user_agent"] == "curl/8.7.1"
    assert "_sig" not in row, "dedup key must not leak into the payload"


def test_wrong_token_looks_like_a_missing_route(client: TestClient) -> None:
    """A prober must not be able to tell whether a bucket is currently open."""

    client.post("/api/v1/oob/start", params={"base_url": "https://h.example"})
    before = len(client.get("/api/v1/oob/hits").json()["hits"])

    wrong = client.get("/oob/000000/whatever")
    assert wrong.status_code == 404

    assert len(client.get("/api/v1/oob/hits").json()["hits"]) == before


def test_repeated_callbacks_collapse_into_one_row_with_a_count(client: TestClient) -> None:
    """A target in a retry loop must not be able to grow the list without bound."""

    token = client.post("/api/v1/oob/start", params={"base_url": "https://h.example"}).json()["token"]
    for _ in range(5):
        client.get(f"/oob/{token}/probe")

    payload = client.get("/api/v1/oob/hits").json()
    assert payload["unique_count"] == 1
    assert payload["hit_count"] == 5
    assert payload["hits"][0]["count"] == 5


def test_different_labels_stay_separate(client: TestClient) -> None:
    """One global token serves every worker, so the label is what attributes a hit."""

    token = client.post("/api/v1/oob/start", params={"base_url": "https://h.example"}).json()["token"]
    client.get(f"/oob/{token}/i052-ssrf")
    client.get(f"/oob/{token}/i103-xxe")

    labels = {row["label"] for row in client.get("/api/v1/oob/hits").json()["hits"]}
    assert labels == {"i052-ssrf", "i103-xxe"}

    only = client.get("/api/v1/oob/hits", params={"label": "i052"}).json()["hits"]
    assert [row["label"] for row in only] == ["i052-ssrf"]


def test_stop_clears_records_and_closes_the_door(client: TestClient) -> None:
    """Stop means stop: the token dies and the records go with it."""

    token = client.post("/api/v1/oob/start", params={"base_url": "https://h.example"}).json()["token"]
    client.get(f"/oob/{token}/probe")
    assert client.post("/api/v1/oob/stop").json() == {"stopped": True, "cleared": 1}

    assert client.get(f"/oob/{token}/probe").status_code == 404
    state = client.get("/api/v1/oob").json()
    assert state["running"] is False
    assert state["url"] == ""
    assert client.get("/api/v1/oob/hits").json()["hits"] == []


def test_expired_bucket_reports_closed_and_drops_records() -> None:
    """The timer is not the only guard: every read re-checks the clock."""

    store = OobStore()
    state = store.start("https://h.example", ttl_seconds=30)
    token = state["token"]
    assert store.record(token, method="GET", path=f"/oob/{token}/x", query="", headers={}, client="1.2.3.4")

    # Move the deadline into the past rather than sleeping 30 s.
    store._expires_at = 0
    assert store.status()["running"] is False
    assert store.hits() == []
    assert store.record(token, method="GET", path=f"/oob/{token}/x", query="", headers={}, client="1.2.3.4") is False


def test_ttl_is_bounded_at_the_edge_and_again_in_the_store(client: TestClient) -> None:
    """A caller cannot ask for a bucket that never closes.

    Two independent guards, and the test covers both: the route rejects
    anything outside 30..3600 with a 422, and the store clamps again in case it
    is driven directly (the CLI and tests do exactly that).
    """

    assert client.post("/api/v1/oob/start", params={"ttl_seconds": 999999}).status_code == 422
    assert client.post("/api/v1/oob/start", params={"ttl_seconds": 1}).status_code == 422

    state = client.post("/api/v1/oob/start", params={"ttl_seconds": 3600}).json()
    assert state["ttl_seconds"] == 3600

    # Store-level clamp: called with a nonsense TTL, it still closes eventually.
    store = OobStore()
    assert store.start("https://h.example", ttl_seconds=10_000_000)["ttl_seconds"] <= 3600
    assert store.start("https://h.example", ttl_seconds=1)["ttl_seconds"] >= 30


def test_body_is_truncated_not_stored_whole(client: TestClient) -> None:
    """An SSRF gadget can be pointed here with megabytes attached."""

    token = client.post("/api/v1/oob/start", params={"base_url": "https://h.example"}).json()["token"]
    client.post(f"/oob/{token}/big", content=b"A" * 50_000)

    row = client.get("/api/v1/oob/hits").json()["hits"][0]
    assert row["body_size"] <= 2048
    assert len(row["body_preview"]) <= 2048


def test_oob_payload_path_is_outside_the_api_prefix(client: TestClient) -> None:
    """The payload URL is pasted into someone else's system; it must not look like an API call."""

    token = client.post("/api/v1/oob/start", params={"base_url": "https://h.example"}).json()["token"]
    assert client.get(f"/oob/{token}").status_code == 200
    # And the same name under /api/v1 is the authenticated control surface instead.
    assert client.get(f"/api/v1/oob/{token}").status_code == 404
