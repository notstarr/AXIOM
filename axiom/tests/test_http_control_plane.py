from __future__ import annotations

import httpx

from axiom.dispatcher.schemas import ClaimedIntent
from axiom.dispatcher.http import ControlPlaneError, HttpControlPlane
from axiom.modules.execution.schemas import ReleaseIntentCommand


def test_http_control_plane_uses_api_only_and_maps_claim_conflict() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/claim"):
            return httpx.Response(409, json={"error": {"message": "already claimed"}})
        return httpx.Response(200, json={"tasks": [], "workers": []})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://axiom.test/api/v1")
    control = HttpControlPlane("http://axiom.test", client=client)

    assert control.claim(task_id="t_001", intent_id="i001", worker_name="w") is None
    snapshot = control.snapshot()

    assert snapshot.tasks == []
    assert snapshot.workers == []
    assert [request.url.path for request in requests] == [
        "/api/v1/dispatch/tasks/t_001/intents/i001/claim",
        "/api/v1/dispatch/snapshot",
    ]


def test_http_control_plane_exposes_non_conflict_as_protocol_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": {"message": "unavailable"}})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://axiom.test/api/v1")
    control = HttpControlPlane("http://axiom.test", client=client)

    try:
        control.snapshot()
    except ControlPlaneError as exc:
        assert exc.status_code == 503
        assert "unavailable" in str(exc)
    else:
        raise AssertionError("expected ControlPlaneError")


def test_http_control_plane_treats_release_conflict_as_idempotent() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={"error": {"code": "lease_inactive", "message": "lease no longer active"}},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://axiom.test/api/v1")
    control = HttpControlPlane("http://axiom.test", client=client)
    work = ClaimedIntent(
        task_id="t_001",
        project_id="prj_001",
        intent_id="i001",
        kind="explore",
        worker_name="worker-a",
        lease_id="lease-a",
        lease_version=1,
        claimed_at="2026-07-23T00:00:00Z",
        description="test",
    )

    control.release(
        work,
        ReleaseIntentCommand(
            lease_id="lease-a",
            failure_kind="lease_heartbeat_lost",
            failure_class="transient",
        ),
    )
