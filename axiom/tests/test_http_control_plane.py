from __future__ import annotations

import json

import httpx
import pytest

from axiom.dispatcher.schemas import ClaimAction, ClaimedIntent
from axiom.dispatcher.heartbeat import LeaseLostError
from axiom.dispatcher.http import (
    CONTROL_PLANE_INPUT_TIMEOUT_SECONDS,
    ControlPlaneError,
    HttpControlPlane,
)
from axiom.modules.execution.schemas import (
    ActivityCommand,
    ExecutionInput,
    ReleaseIntentCommand,
)
from axiom.modules.plan.schemas import ClaimedPlanRun


def test_http_control_plane_claims_plan_through_the_plan_endpoint() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "task_id": "t_001",
                "plan_run_id": "pr000001",
                "worker_name": "plan-worker",
                "kind": "plan",
                "lease_id": "lease-plan",
                "claimed_at": "2026-08-21T00:00:00Z",
                "status": "claimed",
            },
        )

    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="http://axiom.test/api/v1",
    )
    control = HttpControlPlane("http://axiom.test", client=client)

    claimed = control.claim_work(
        ClaimAction(
            task_id="t_001",
            plan_run_id="pr000001",
            worker_name="plan-worker",
            kind="plan",
        )
    )

    assert isinstance(claimed, ClaimedPlanRun)
    assert requests[0].url.path == (
        "/api/v1/dispatch/tasks/t_001/plan-runs/pr000001/claim"
    )
    assert json.loads(requests[0].content) == {"worker_name": "plan-worker"}


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


def test_http_control_plane_input_uses_extended_timeout() -> None:
    observed_timeouts: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed_timeouts.append(request.extensions["timeout"]["read"])
        payload = ExecutionInput(
            task_id="t_001",
            intent_id="i001",
            lease_id="lease-a",
            worker_name="worker-a",
            origin="https://a.example.com",
            goal="完成测试",
            scope={"enabled": False, "targets": [], "exclude": []},
            run_dir="/tmp/axiom-test",
        ).model_dump(mode="json")
        return httpx.Response(200, json=payload)

    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="http://axiom.test/api/v1",
        timeout=20,
    )
    control = HttpControlPlane("http://axiom.test", client=client)
    work = ClaimedIntent(
        task_id="t_001",
        intent_id="i001",
        kind="bootstrap",
        worker_name="worker-a",
        lease_id="lease-a",
        claimed_at="2026-08-10T00:00:00Z",
    )

    result = control.input(work)

    assert result.task_id == "t_001"
    assert observed_timeouts == [CONTROL_PLANE_INPUT_TIMEOUT_SECONDS]
