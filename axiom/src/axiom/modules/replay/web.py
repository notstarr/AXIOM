"""The replay page: one POST to send, one GET to look at it.

The reviewer's loop is short and fixed:

1. ``POST /api/send`` with a packet (or a curl command) — the service really sends it
   and answers with a URL.
2. ``playwright`` opens that URL.  The page is the exchange, drawn; there is nothing to
   click, because the screenshot is the point and any control would end up in it.
3. The reviewer captures the page and hangs the file on a step with
   ``axiom review evidence attach``.

So this app has exactly two routes that matter, plus health.  Earlier versions had a
console with pickers and buttons; it was the wrong shape — the reviewer already knows
which step it is working on, and every control is something that can appear in the
photograph.

Why a second process instead of a route on 8888: the page has to open without an AXIOM
login (a reviewer photographing a packet should not also manage an account), and
"reachable without login" on a public host means "reachable by everyone".  Binding
127.0.0.1 and *not* adding it to Caddy turns the SSH tunnel into the access boundary.
A token closes the remaining gap, because loopback is not a privilege boundary on a
shared box.  Paths are under ``/x/`` so nothing here collides with AXIOM's admin API.
"""

from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from axiom.modules.replay import render as render_module
from axiom.modules.replay import store as store_module
from axiom.modules.replay.service import ReplayError, ReplayService


TOKEN_ENV = "AXIOM_REPLAY_TOKEN"
DEFAULT_TOKEN_FILE = Path("/var/lib/axiom/replay-token")


def read_or_create_token(data_dir: Path) -> str:
    """One token, stored 0600, re-read on start so a restart does not break links."""
    configured = os.environ.get(TOKEN_ENV, "").strip()
    if configured:
        return configured

    candidates = [DEFAULT_TOKEN_FILE, data_dir / "replay-token"]
    for path in candidates:
        try:
            existing = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if existing:
            return existing

    token = secrets.token_urlsafe(24)
    for path in candidates:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(token + "\n", encoding="utf-8")
            path.chmod(0o600)
            return token
        except OSError:
            continue
    return token


class SendRequest(BaseModel):
    # Either a recorded step (preferred: the packet comes from the database) or a
    # packet/curl pasted by the reviewer for free-form probing.
    task_id: str
    result_id: str = ""
    step: int = 0
    packet: str = ""
    highlights: list[str] = Field(default_factory=list)
    follow_redirects: bool = False
    allow_delete: bool = False
    allow_private: bool = False


class SendResponse(BaseModel):
    ok: bool = True
    url: str
    status_code: int | None = None
    reason: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    body: str = ""
    body_truncated: bool = False
    elapsed_ms: int = 0
    scheme_used: str = ""
    compare: dict[str, Any] = Field(default_factory=dict)
    expires_in_seconds: int = store_module.TTL_SECONDS


class ErrorResponse(BaseModel):
    ok: bool = False
    message: str
    hint: str = ""


def create_replay_app(
    *,
    container: Any,
    token: str,
    data_dir: Path,
    public_base: str = "",
) -> FastAPI:
    app = FastAPI(title="AXIOM Replay", docs_url=None, redoc_url=None, openapi_url=None)
    service = ReplayService(
        container.engine,
        tasks=container.tasks,
        red_team=container.red_team,
        evidence=container.report_evidence,
        vulnerabilities=container.vulnerabilities,
    )
    app.state.replay = service
    app.state.store = store_module.store()

    def require_token(
        authorization: str = Header(default=""),
        t: str = Query(default=""),
    ) -> None:
        presented = ""
        if authorization.lower().startswith("bearer "):
            presented = authorization.split(" ", 1)[1].strip()
        elif t:
            presented = t.strip()
        if not presented or not hmac.compare_digest(presented, token):
            raise HTTPException(status_code=401, detail="token 无效")

    def base_url(request: Request) -> str:
        if public_base:
            return public_base.rstrip("/")
        return str(request.base_url).rstrip("/")

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "service": "axiom-replay", **store_module.store().stats()}

    # ── 1. send, and get a URL back ──────────────────────────────────────────

    @app.post("/api/send", response_model=SendResponse, dependencies=[Depends(require_token)])
    def send(body: SendRequest, request: Request) -> SendResponse:
        try:
            outcome = service.replay(
                task_id=body.task_id,
                result_id=body.result_id,
                step=body.step,
                packet_text=body.packet,
                highlights=body.highlights,
                follow_redirects=body.follow_redirects,
                allow_delete=body.allow_delete,
                allow_private=body.allow_private,
                capture=False,
                attach=False,
                actor="replay.tool",
            )
        except ReplayError as exc:
            raise HTTPException(
                status_code=exc.status, detail={"message": exc.message, "hint": exc.hint}
            ) from exc

        if not outcome.sent:
            raise HTTPException(
                status_code=502,
                detail={
                    "message": f"未能发送：{outcome.live.error_kind} — {outcome.live.error_message}",
                    "hint": "",
                },
            )

        live = outcome.live
        response_text = render_module.response_text_of(
            live.status_code, live.reason, live.headers, live.body
        )
        entry = store_module.store().put(
            request_text=outcome.request_text,
            response_text=response_text,
            summary={
                # A free-form send has no recorded response to compare against; saying
                # "无法比对" on the picture is noise, so only carry a real verdict.
                "title": outcome.step_title or _first_line(outcome.request_text),
                "show_compare": outcome.step > 0,
                "elapsed_ms": live.elapsed_ms,
                "scheme_used": live.scheme_used,
                "scheme_fell_back": live.scheme_fell_back,
                "body_truncated": live.body_truncated,
                "body_bytes": live.body_bytes,
                "compare": outcome.comparison.to_payload(),
            },
        )
        return SendResponse(
            url=f"{base_url(request)}/x/{entry.key}?t={token}",
            status_code=live.status_code,
            reason=live.reason,
            headers=live.headers,
            body=live.body,
            body_truncated=live.body_truncated,
            elapsed_ms=live.elapsed_ms,
            scheme_used=live.scheme_used,
            compare=outcome.comparison.to_payload(),
        )

    # ── 2. look at it ────────────────────────────────────────────────────────

    @app.get("/x/{key}", response_class=HTMLResponse)
    def exchange_page(key: str, t: str = Query(default="")) -> HTMLResponse:
        """The document itself.  No controls, no state, nothing to click.

        The token is still required so a stray key on the host cannot be read by another
        local process; it arrives in the URL the tool handed out.
        """
        if not t or not hmac.compare_digest(t, token):
            raise HTTPException(status_code=401, detail="token 无效")
        entry = store_module.store().get(key)
        if entry is None:
            return HTMLResponse(_EXPIRED_HTML, status_code=410)
        summary = entry.summary or {}
        payload = render_module.RenderInput(
            packet_text=entry.request_text,
            response_text=entry.response_text,
            title=str(summary.get("title") or "HTTP"),
            source_label="axiom-replay",
            elapsed_ms=summary.get("elapsed_ms"),
            comparison=(
                _comparison_of(summary.get("compare"))
                if summary.get("show_compare", True)
                else None
            ),
            body_truncated=bool(summary.get("body_truncated")),
            original_body_bytes=int(summary.get("body_bytes") or 0),
            scheme_note=_scheme_note(summary),
        )
        return HTMLResponse(render_module.build_html(payload))

    # ── 3. hang the picture on a step ─────────────────────────────────────────

    @app.post("/api/evidence", dependencies=[Depends(require_token)])
    async def attach_evidence(
        request: Request,
        task_id: str = Form(...),
        subject_type: str = Form(default="redteam_result"),
        subject_id: str = Form(...),
        step: int = Form(default=0),
        kind: str = Form(default="packet"),
        caption: str = Form(default=""),
        file: UploadFile = File(...),
    ) -> dict:
        """Upload a picture and hang it on a step, in one call.

        The reviewer does not need to know that artifacts and attachments are separate
        things; it hands over bytes and gets an id back.  The download URL is returned as
        well, because a markdown report has to embed something the reader can resolve —
        an id alone renders as a broken image.
        """
        if subject_type not in {"redteam_result", "vulnerability"}:
            raise HTTPException(status_code=400, detail="subject_type 只能是 redteam_result 或 vulnerability")
        if kind not in {"packet", "business"}:
            raise HTTPException(status_code=400, detail="kind 只能是 packet 或 business")
        content = await file.read()
        if not content:
            raise HTTPException(status_code=400, detail="文件为空")
        try:
            record = service.capture_evidence(
                task_id=task_id,
                subject_type=subject_type,
                subject_id=subject_id,
                filename=file.filename or "shot.png",
                content=content,
                step=step,
                kind=kind,
                caption=caption,
                actor="replay.tool",
            )
        except ReplayError as exc:
            raise HTTPException(
                status_code=exc.status, detail={"message": exc.message, "hint": exc.hint}
            ) from exc
        except Exception as exc:  # conflict from the evidence layer (step out of range)
            raise HTTPException(
                status_code=409, detail={"message": str(exc), "hint": ""}
            ) from exc
        artifact_id = record.get("artifact_id") or ""
        record["download_url"] = (
            f"{base_url(request).replace(':8890', ':8888')}"
            f"/api/v1/tasks/{task_id}/artifacts/{artifact_id}/download"
        )
        return record

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, dict):
            message = str(detail.get("message") or "")
            hint = str(detail.get("hint") or "")
        else:
            message = str(detail)
            hint = ""
        return JSONResponse(
            status_code=exc.status_code,
            content=ErrorResponse(message=message, hint=hint).model_dump(),
        )

    return app


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return "HTTP"


def _comparison_of(raw: Any):
    from axiom.modules.replay.compare import Check, Comparison

    if not isinstance(raw, dict) or not raw.get("status"):
        return None
    checks = [
        Check(
            name=str(item.get("name") or ""),
            expected=str(item.get("expected") or ""),
            actual=str(item.get("actual") or ""),
            ok=item.get("ok"),
        )
        for item in raw.get("checks") or []
        if isinstance(item, dict)
    ]
    return Comparison(
        status=str(raw["status"]),
        checks=checks,
        notes=[str(item) for item in raw.get("notes") or []],
    )


def _scheme_note(summary: dict[str, Any]) -> str:
    scheme = str(summary.get("scheme_used") or "")
    if not scheme:
        return ""
    if summary.get("scheme_fell_back"):
        return f"https 不可用，实际使用 {scheme}"
    return f"实际使用 {scheme}"


_EXPIRED_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>已过期</title>
<style>
  body { margin:0; height:100vh; display:flex; align-items:center; justify-content:center;
         background:#fff; color:#64748b;
         font-family: ui-monospace, Menlo, "WenQuanYi Zen Hei", monospace; font-size:13px; }
</style></head>
<body>这次报文已过期（保留 10 分钟）。重新发送即可得到新的地址。</body></html>"""


__all__ = ["SendRequest", "SendResponse", "create_replay_app", "read_or_create_token"]
