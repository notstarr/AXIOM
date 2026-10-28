"""Out-of-band capture routes.

Two routers live here on purpose, because they sit on opposite sides of the
auth boundary:

* ``router``        mounted under ``/api/v1`` — authenticated start/stop/read.
* ``public_router`` mounted at ``/oob`` without a prefix — the payload target.
  Anyone who learns a live token can write a hit; nobody can read one without
  a service token or a logged-in session.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request, Response

from axiom.modules.oob.store import STORE, public_base_url

router = APIRouter(tags=["oob"])

public_router = APIRouter(tags=["oob-public"], include_in_schema=False)


# ── authenticated lifecycle ───────────────────────────────────────────────
@router.post("/oob/start")
def start_oob(
    ttl_seconds: int = Query(default=600, ge=30, le=3600),
    base_url: str = Query(default=""),
) -> dict:
    """Open the bucket, or extend the open one and hand back the same URL."""

    return STORE.start(base_url.strip() or public_base_url(), ttl_seconds=ttl_seconds)


@router.get("/oob")
def oob_status() -> dict:
    return STORE.status()


@router.get("/oob/hits")
def oob_hits(label: str = Query(default=""), limit: int = Query(default=200, ge=1, le=500)) -> dict:
    state = STORE.status()
    return {**state, "hits": STORE.hits(label=label or None, limit=limit)}


@router.post("/oob/stop")
def stop_oob() -> dict:
    return STORE.stop()


# ── public capture ────────────────────────────────────────────────────────
_PUBLIC_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


def _capture(request: Request, token: str, label_path: str) -> Response:
    # Body is read capped, not stored whole: an SSRF gadget can be pointed at
    # this route with megabytes attached, and the useful part is the headers
    # plus the first couple of kilobytes anyway.
    raw = request.scope.get("oob_body", b"")
    ok = STORE.record(
        token,
        method=request.method,
        path=f"/oob/{token}" + (f"/{label_path}" if label_path else ""),
        query=request.url.query or "",
        headers={k.lower(): v for k, v in request.headers.items()},
        client=request.client.host if request.client else "",
        label=label_path,
        body=raw,
    )
    if not ok:
        # Indistinguishable from "no such route": a prober must not be able to
        # tell whether a bucket is open right now.
        return Response(status_code=404, content=b"Not Found", media_type="text/plain")
    # Never echo anything back.  Reflecting request content would turn this
    # endpoint into a cross-site scripting primitive of its own.
    return Response(status_code=200, content=b"ok", media_type="text/plain")


@public_router.api_route("/oob/{token}", methods=_PUBLIC_METHODS)
async def oob_capture_root(request: Request, token: str) -> Response:
    request.scope["oob_body"] = (await request.body())[:2048]
    return _capture(request, token, "")


@public_router.api_route("/oob/{token}/{label_path:path}", methods=_PUBLIC_METHODS)
async def oob_capture_labeled(request: Request, token: str, label_path: str) -> Response:
    request.scope["oob_body"] = (await request.body())[:2048]
    return _capture(request, token, label_path)
