from __future__ import annotations

import json

import httpx
import pytest

from axiom.dispatcher.schemas import ClaimedIntent
from axiom.dispatcher.heartbeat import LeaseLostError
from axiom.dispatcher.http import ControlPlaneError, HttpControlPlane
from axiom.modules.execution.schemas import ReleaseIntentCommand
from axiom.modules.execution.schemas import ActivityCommand


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


def test_http_control_plane_maps_inactive_heartbeat_to_lease_loss() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={"error": {"code": "lease_inactive", "message": "task stopped"}},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://axiom.test/api/v1")
    control = HttpControlPlane("http://axiom.test", client=client)
    work = ClaimedIntent(
        task_id="t_001",
        intent_id="i001",
        kind="explore",
        worker_name="worker-a",
        lease_id="lease-a",
        claimed_at="2026-07-24T00:00:00Z",
    )

    with pytest.raises(LeaseLostError):
        control.heartbeat(work)


def test_http_control_plane_reports_activity() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204)

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://axiom.test/api/v1")
    control = HttpControlPlane("http://axiom.test", client=client)
    work = ClaimedIntent(
        task_id="t_001",
        intent_id="i001",
        kind="bootstrap",
        worker_name="worker-a",
        lease_id="lease-a",
        claimed_at="2026-07-24T00:00:00Z",
    )

    control.activity(
        work,
        ActivityCommand(
            lease_id="lease-a",
            activity_kind="tool_running",
            activity_summary="正在执行 nmap 端口与服务扫描",
            activity_tool="nmap",
        ),
    )

    assert requests[0].url.path == "/api/v1/dispatch/tasks/t_001/intents/i001/activity"
    body = json.loads(requests[0].content)
    assert body["lease_id"] == "lease-a"
    assert body["activity_tool"] == "nmap"


def test_http_control_plane_maps_inactive_activity_lease_to_lease_loss() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={"error": {"code": "lease_inactive", "message": "task stopped"}},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://axiom.test/api/v1")
    control = HttpControlPlane("http://axiom.test", client=client)
    work = ClaimedIntent(
        task_id="t_001",
        intent_id="i001",
        kind="explore",
        worker_name="worker-a",
        lease_id="lease-a",
        claimed_at="2026-07-24T00:00:00Z",
    )

    with pytest.raises(LeaseLostError):
        control.activity(
            work,
            ActivityCommand(
                lease_id="lease-a",
                activity_kind="tool_running",
                activity_summary="正在执行 nmap 端口与服务扫描",
                activity_tool="nmap",
            ),
        )
