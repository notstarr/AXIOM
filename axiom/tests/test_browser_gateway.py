from __future__ import annotations

import json
from http.client import HTTPConnection

from axiom.dispatcher.browser_broker import BrowserGatewayServer


class _FakeManager:
    def __init__(self) -> None:
        self._lease_lock = __import__("threading").Lock()
        self._lease_tokens = {"lease-1": "lease-token"}
        self.calls: list[dict[str, object]] = []

    def _request(self, payload: dict[str, object], *, timeout: float = 15.0):
        self.calls.append(payload)
        return {"ok": True, "content": "ok", "data": {"timeout": timeout}}


def _call(port: int, headers: dict[str, str], payload: dict[str, object]):
    connection = HTTPConnection("127.0.0.1", port, timeout=3)
    connection.request(
        "POST",
        "/internal/browser/v1/call",
        body=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json", **headers},
    )
    response = connection.getresponse()
    body = json.loads(response.read().decode("utf-8"))
    connection.close()
    return response.status, body


def test_browser_gateway_requires_service_and_lease_credentials() -> None:
    manager = _FakeManager()
    gateway = BrowserGatewayServer(
        manager=manager,
        bind="127.0.0.1",
        port=0,
        service_token="runner-token",
    )
    gateway.start()
    try:
        status, body = _call(
            gateway.port,
            {"Authorization": "Bearer wrong"},
            {"op": "browser_list_pages", "token": "lease-token", "params": {}},
        )
        assert status == 401
        assert body["error"]["code"] == "forbidden"

        status, body = _call(
            gateway.port,
            {
                "Authorization": "Bearer runner-token",
                "X-Axiom-Browser-Lease": "lease-1",
            },
            {"op": "browser_list_pages", "token": "wrong", "params": {}},
        )
        assert status == 403
        assert body["error"]["code"] == "forbidden"

        status, body = _call(
            gateway.port,
            {
                "Authorization": "Bearer runner-token",
                "X-Axiom-Browser-Lease": "lease-1",
                "X-Axiom-Browser-Token": "lease-token",
            },
            {"op": "browser_list_pages", "params": {}},
        )
        assert status == 200
        assert body["ok"] is True
        assert manager.calls == [
            {"op": "browser_list_pages", "params": {}, "token": "lease-token"}
        ]
    finally:
        gateway.close()
