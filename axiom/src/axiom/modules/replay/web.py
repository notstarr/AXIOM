"""The 8890 app: a loopback-only replay console.

Why a second process instead of a route on 8888:

The console has to be reachable without an AXIOM login (a reviewer opening a picture
should not also have to manage an account), and "reachable without login" on a public
host means "reachable by everyone".  Binding 127.0.0.1 and *not* adding it to Caddy
turns the SSH tunnel into the access boundary — no port on the internet, nothing to
misconfigure upstream.

A long-lived token closes the remaining gap: loopback is not a privilege boundary on a
shared VPS, so any local process could otherwise drive this service.
"""

from __future__ import annotations

import base64
import hmac
import os
import secrets
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from axiom.modules.replay.schemas import (
    ErrorResponse,
    ReplayRequest,
    ReplayResponse,
    ResultInfo,
    StepInfo,
    TaskInfo,
)
from axiom.modules.replay.service import ReplayError, ReplayService
from axiom.shared.errors import DomainError


TOKEN_ENV = "AXIOM_REPLAY_TOKEN"
DEFAULT_TOKEN_FILE = Path("/var/lib/axiom/replay-token")


class ApiError(BaseModel):
    ok: bool = False
    message: str
    hint: str = ""


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
    # Nothing writable: fall back to an in-memory token rather than running open.
    return token


def create_replay_app(
    *,
    container: Any,
    token: str,
    data_dir: Path,
) -> FastAPI:
    app = FastAPI(
        title="AXIOM Replay",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    service = ReplayService(
        container.engine,
        tasks=container.tasks,
        red_team=container.red_team,
        evidence=container.report_evidence,
    )
    app.state.replay = service

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

    # ── pages ────────────────────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse(_INDEX_HTML)

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "service": "axiom-replay"}

    # ── data for the pickers ─────────────────────────────────────────────────

    @app.get("/api/tasks", response_model=list[TaskInfo], dependencies=[Depends(require_token)])
    def list_tasks() -> list[TaskInfo]:
        items = container.tasks.list()
        red_team = [item for item in items if item.engagement_mode == "red_team"]
        return [TaskInfo(id=item.id, title=item.title) for item in red_team]

    @app.get("/api/results", response_model=list[ResultInfo], dependencies=[Depends(require_token)])
    def list_results(task_id: str) -> list[ResultInfo]:
        try:
            results = service.red_team.list_results(task_id)
        except DomainError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        payload: list[ResultInfo] = []
        for item in results:
            steps: list[StepInfo] = []
            if item.reproduction is not None:
                steps = [
                    StepInfo(
                        index=index,
                        title=step.title,
                        request=step.request,
                        response=step.response,
                        proves=step.proves,
                    )
                    for index, step in enumerate(item.reproduction.steps, start=1)
                ]
            payload.append(
                ResultInfo(
                    id=item.id,
                    title=item.title,
                    status=item.status,
                    target_asset=item.target_asset,
                    step_count=len(steps),
                    steps=steps,
                )
            )
        return payload

    # ── the one action ───────────────────────────────────────────────────────

    @app.post("/api/replay", response_model=ReplayResponse, dependencies=[Depends(require_token)])
    def replay(body: ReplayRequest) -> ReplayResponse:
        try:
            outcome = service.replay(
                task_id=body.task_id,
                result_id=body.result_id,
                step=body.step,
                packet_text=body.packet,
                highlights=body.highlights,
                asserts=body.asserts,
                caption=body.caption,
                follow_redirects=body.follow_redirects,
                allow_delete=body.allow_delete,
                allow_private=body.allow_private,
                capture=body.capture,
                attach=body.attach,
                actor="replay.console",
            )
        except ReplayError as exc:
            raise HTTPException(
                status_code=exc.status,
                detail={"message": exc.message, "hint": exc.hint},
            ) from exc
        except DomainError as exc:
            raise HTTPException(status_code=409, detail={"message": str(exc), "hint": ""}) from exc

        payload = outcome.to_payload()
        return ReplayResponse(
            request_text=payload["request_text"],
            step_title=payload["step_title"],
            sent=payload["sent"],
            response=payload["response"],
            compare=payload["compare"],
            artifact_id=payload["artifact_id"],
            evidence_id=payload["evidence_id"],
            screenshot_error=payload["screenshot_error"],
            image_base64=outcome.image_base64,
            notes=payload["notes"],
        )

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


_INDEX_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>AXIOM 重放器</title>
<style>
  * { box-sizing: border-box; }
  body { margin:0; font-family: ui-monospace, SFMono-Regular, Menlo, "WenQuanYi Zen Hei", monospace;
         font-size:13px; color:#0f172a; background:#eef1f5; }
  header { background:#0f172a; color:#f8fafc; padding:10px 16px; display:flex; gap:12px; align-items:center; }
  header b { font-size:14px; }
  header .sp { flex:1; }
  .wrap { padding:16px; display:flex; flex-direction:column; gap:12px; }
  .bar { display:flex; gap:8px; align-items:center; flex-wrap:wrap; background:#fff;
         border:1px solid #cbd5e1; border-radius:8px; padding:10px 12px; }
  select, input, textarea, button { font:inherit; }
  select, input[type=text] { border:1px solid #cbd5e1; border-radius:6px; padding:5px 8px; background:#fff; }
  button { border:1px solid #cbd5e1; background:#fff; border-radius:6px; padding:6px 14px; cursor:pointer; }
  button.primary { background:#1d4ed8; border-color:#1d4ed8; color:#fff; font-weight:700; }
  button:disabled { opacity:.5; cursor:not-allowed; }
  .cols { display:flex; gap:12px; align-items:stretch; }
  .col { width:50%; min-width:0; background:#fff; border:1px solid #cbd5e1; border-radius:8px; overflow:hidden; }
  .col h3 { margin:0; padding:6px 12px; font-size:11px; letter-spacing:.04em; text-transform:uppercase; }
  .req h3 { background:#eff6ff; color:#1d4ed8; }
  .res h3 { background:#ecfdf5; color:#047857; }
  textarea, pre { margin:0; padding:12px; width:100%; min-height:340px; border:0; font:inherit;
                  white-space:pre-wrap; word-break:break-all; background:transparent; }
  textarea { resize:vertical; outline:none; }
  pre { overflow:auto; }
  .verdict { background:#fff; border:1px solid #cbd5e1; border-radius:8px; padding:10px 14px; }
  .badge { font-weight:700; padding:2px 8px; border-radius:4px; margin-right:10px; }
  .match { background:#d1fae5; color:#065f46; }
  .mismatch { background:#fee2e2; color:#991b1b; }
  .inconclusive { background:#fef3c7; color:#92400e; }
  .check { margin-right:14px; color:#334155; }
  .check.bad { color:#b91c1c; font-weight:700; }
  .err { background:#fee2e2; color:#991b1b; border:1px solid #fecaca; border-radius:8px; padding:10px 14px; }
  .hint { color:#92400e; margin-top:4px; }
  .shot { background:#fff; border:1px solid #cbd5e1; border-radius:8px; padding:10px; }
  .shot img { max-width:100%; display:block; border:1px solid #e2e8f0; border-radius:4px; }
  .muted { color:#64748b; }
</style></head>
<body>
<header><b>AXIOM 重放器</b><span class="muted" style="color:#94a3b8">仅本机 · 报文来自上报记录</span>
  <span class="sp"></span>
  <input id="token" type="text" placeholder="token" style="width:220px">
</header>
<div class="wrap">
  <div class="bar">
    <span>任务</span><select id="task" style="min-width:220px"><option value="">载入中…</option></select>
    <span>成果</span><select id="result" style="min-width:260px"><option value="">—</option></select>
    <span>步骤</span><select id="step" style="min-width:220px"><option value="">—</option></select>
  </div>
  <div class="bar">
    <span>高亮</span><input id="hl" type="text" placeholder="逗号分隔，如 &quot;code&quot;:0" style="flex:1;min-width:180px">
    <span>说明</span><input id="cap" type="text" placeholder="进报告的图注" style="flex:1;min-width:180px">
  </div>
  <div class="cols">
    <div class="col req"><h3>请求（可改）</h3><textarea id="req" spellcheck="false"></textarea></div>
    <div class="col res"><h3>响应</h3><pre id="res">（尚未发送）</pre></div>
  </div>
  <div class="bar">
    <button id="send">发送</button>
    <button id="shot" class="primary">截图并挂证据</button>
    <span id="status" class="muted"></span>
  </div>
  <div id="verdict"></div>
  <div id="shotbox"></div>
</div>
<script>
const $ = id => document.getElementById(id);
let RESULTS = [];

function token() {
  let t = $('token').value.trim();
  if (!t) { t = localStorage.getItem('axiom-replay-token') || ''; $('token').value = t; }
  return t;
}
function saveToken() { localStorage.setItem('axiom-replay-token', $('token').value.trim()); }
$('token').addEventListener('change', () => { saveToken(); loadTasks(); });

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: { 'content-type': 'application/json', 'authorization': 'Bearer ' + token(), ...(opts.headers||{}) },
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg = body.message || body.detail || '请求失败';
    const hint = body.hint || '';
    throw Object.assign(new Error(msg), { hint });
  }
  return body;
}

function opts(list, pick) {
  return list.map(x => `<option value="${pick.value(x)}">${pick.label(x)}</option>`).join('');
}

async function loadTasks() {
  if (!token()) return;
  try {
    const tasks = await api('/api/tasks');
    $('task').innerHTML = '<option value="">—</option>' + opts(tasks, {value: t => t.id, label: t => `${t.id} · ${t.title}`});
  } catch (e) { showError(e); }
}

$('task').addEventListener('change', async () => {
  const taskId = $('task').value;
  $('result').innerHTML = '<option value="">—</option>';
  $('step').innerHTML = '<option value="">—</option>';
  $('req').value = ''; $('res').textContent = '（尚未发送）'; $('verdict').innerHTML = ''; $('shotbox').innerHTML = '';
  if (!taskId) return;
  try {
    RESULTS = await api('/api/results?task_id=' + encodeURIComponent(taskId));
    $('result').innerHTML = '<option value="">—</option>' + opts(RESULTS, {
      value: r => r.id, label: r => `${r.id} · ${r.title}（${r.step_count} 步）`});
  } catch (e) { showError(e); }
});

$('result').addEventListener('change', () => {
  const result = RESULTS.find(r => r.id === $('result').value);
  $('step').innerHTML = '<option value="">—</option>';
  if (!result) return;
  $('step').innerHTML = opts(result.steps, {value: s => s.index, label: s => `${s.index}. ${s.title}`});
});

$('step').addEventListener('change', () => {
  const result = RESULTS.find(r => r.id === $('result').value);
  const step = result && result.steps.find(s => String(s.index) === $('step').value);
  if (!step) return;
  $('req').value = step.request;
  $('res').textContent = '（上报的响应）\\n\\n' + step.response;
  $('verdict').innerHTML = ''; $('shotbox').innerHTML = '';
});

function payload(action) {
  const resultId = $('result').value;
  const step = Number($('step').value || 0);
  const body = {
    action,
    task_id: $('task').value,
    result_id: resultId,
    step: step,
    highlights: $('hl').value.split(',').map(s => s.trim()).filter(Boolean),
    caption: $('cap').value.trim(),
  };
  if (!resultId || !step) body.packet = $('req').value;
  return body;
}

async function run(action) {
  if (!token()) { showError(new Error('先填 token：sudo cat /var/lib/axiom/replay-token')); return; }
  $('send').disabled = true; $('shot').disabled = true;
  $('status').textContent = '发送中…';
  $('verdict').innerHTML = ''; $('shotbox').innerHTML = '';
  try {
    const out = await api('/api/replay', { method: 'POST', body: JSON.stringify(payload(action)) });
    renderResult(out);
    $('status').textContent = out.sent ? `完成（${out.response.elapsed_ms} ms）` : '未能发送';
  } catch (e) {
    showError(e);
    $('status').textContent = '';
  } finally {
    $('send').disabled = false; $('shot').disabled = false;
  }
}

function renderResult(out) {
  if (out.sent) {
    const h = out.response.headers || {};
    const head = `HTTP/1.1 ${out.response.status_code} ${out.response.reason || ''}`.trim();
    const lines = Object.entries(h).map(([k, v]) => `${k}: ${v}`);
    $('res').textContent = [head, ...lines].join('\\n') + '\\n\\n' + (out.response.body || '');
  } else {
    $('res').textContent = `未能发送：${out.response.error_kind} — ${out.response.error_message}`;
  }
  const cmp = out.compare || {};
  if (cmp.status) {
    const labels = { match: '与上报一致', mismatch: '与上报不一致', inconclusive: '无法比对' };
    let html = `<div class="verdict"><span class="badge ${cmp.status}">${labels[cmp.status] || cmp.status}</span>`;
    for (const c of cmp.checks || []) {
      if (c.ok === true) html += `<span class="check">${c.name} ✓</span>`;
      else if (c.ok === false) html += `<span class="check bad">${c.name} ✗（期望 ${c.expected}，实际 ${c.actual}）</span>`;
    }
    html += '</div>';
    for (const n of cmp.notes || []) html += `<div class="verdict hint">${n}</div>`;
    $('verdict').innerHTML = html;
  }
  if (out.screenshot_error) {
    $('shotbox').innerHTML = `<div class="err">截图未生成：${out.screenshot_error}<div class="hint">发包与比对结论不受影响</div></div>`;
  } else if (out.image_base64) {
    const note = out.artifact_id
      ? `<div class="muted" style="margin-bottom:8px">已挂为该成果第 ${$('step').value} 步的报文截图（${out.artifact_id}）</div>`
      : '<div class="muted" style="margin-bottom:8px">截图已生成（未挂载）</div>';
    $('shotbox').innerHTML = `<div class="shot">${note}<img alt="replay" src="data:image/png;base64,${out.image_base64}"></div>`;
  }
}

function showError(e) {
  $('verdict').innerHTML = `<div class="err">${e.message}${e.hint ? `<div class="hint">${e.hint}</div>` : ''}</div>`;
}

$('send').addEventListener('click', () => run('verify'));
$('shot').addEventListener('click', () => run('shot'));
$('token').value = localStorage.getItem('axiom-replay-token') || '';
loadTasks();
</script>
</body></html>"""


__all__ = ["create_replay_app", "read_or_create_token"]
