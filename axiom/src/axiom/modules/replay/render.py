"""Render a replay as the packet screenshot a report needs.

Reports get submitted to clients, and a client reads a plain HTTP exchange far more
readily than a paragraph describing one.  The picture is generated from what was
actually sent and actually returned — never from text a worker supplied — so the
screenshot cannot show something the target did not do.

This module owns the only HTML in the replay path.  Callers supply data (packet
text, response text, highlight fragments, a caption); they never supply markup,
which keeps the renderer from becoming a general-purpose HTML-to-image service.
"""

from __future__ import annotations

import html
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from axiom.modules.replay.compare import Comparison
from axiom.modules.replay.sender import SendResult


RENDER_WIDTH = 1440
DEVICE_SCALE = 2
SCREENSHOT_TIMEOUT_MS = 30_000

BEIJING = timezone(timedelta(hours=8))

# One chromium at a time.  The VPS has ~3.6G total and a single instance costs a
# few hundred MB; running two in parallel during an engagement is how the API
# process gets OOM-killed.
_RENDER_LOCK = threading.Lock()


class RenderError(RuntimeError):
    """Chromium is missing or failed; the caller still has a usable send result."""


@dataclass
class RenderInput:
    packet_text: str
    response_text: str
    title: str = ""
    caption: str = ""
    sent_at: datetime | None = None
    source_label: str = ""
    elapsed_ms: int | None = None
    highlights: list[str] = field(default_factory=list)
    comparison: Comparison | None = None
    body_truncated: bool = False
    original_body_bytes: int = 0
    scheme_note: str = ""


def _beijing_now() -> datetime:
    return datetime.now(BEIJING)


def _escape(text: str) -> str:
    return html.escape(text, quote=False)


def _highlight(text: str, fragments: list[str]) -> str:
    """Escape first, then wrap matches. Fragments are matched on the escaped text."""
    escaped = _escape(text)
    for fragment in sorted({item for item in fragments if item}, key=len, reverse=True):
        needle = _escape(fragment)
        if not needle:
            continue
        escaped = escaped.replace(needle, f"<mark>{needle}</mark>")
    # A fragment can appear inside a tag we just inserted (``mark`` contains no
    # ``<``), so a second pass would only risk nesting; one pass is enough.
    return escaped


def build_html(payload: RenderInput) -> str:
    """The whole document. Kept readable rather than compact: it is the report art."""
    sent_at = payload.sent_at or _beijing_now()
    stamp = sent_at.astimezone(BEIJING).strftime("%Y-%m-%d %H:%M:%S")
    title = payload.title or "HTTP 重放"
    meta_bits = [f"{stamp} (UTC+8)"]
    if payload.source_label:
        meta_bits.append(payload.source_label)
    meta = " · ".join(meta_bits)

    request_html = _highlight(payload.packet_text, payload.highlights)
    response_html = _highlight(payload.response_text, payload.highlights)

    elapsed = ""
    if payload.elapsed_ms is not None:
        elapsed = f'<span class="elapsed">{payload.elapsed_ms} ms</span>'

    truncation = ""
    if payload.body_truncated:
        original_kb = max(1, round(payload.original_body_bytes / 1024))
        truncation = f'<div class="truncated">响应体已截断（原始 {original_kb} KB）</div>'

    scheme_note = ""
    if payload.scheme_note:
        scheme_note = f'<div class="note">{_escape(payload.scheme_note)}</div>'

    verdict = ""
    if payload.comparison is not None:
        verdict = _verdict_html(payload.comparison)

    caption = ""
    if payload.caption:
        caption = f'<div class="caption">{_escape(payload.caption)}</div>'

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<style>
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 20px; background: #eef1f5;
    font-family: ui-monospace, SFMono-Regular, Menlo, "WenQuanYi Zen Hei", monospace;
    font-size: 13px; line-height: 1.55; color: #0f172a;
  }}
  .frame {{ background: #fff; border: 1px solid #cbd5e1; border-radius: 8px; overflow: hidden; }}
  .head {{ padding: 10px 16px; background: #f8fafc; border-bottom: 1px solid #e2e8f0; }}
  .head .title {{ font-weight: 700; font-size: 14px; }}
  .head .meta {{ color: #64748b; font-size: 11px; margin-top: 2px; }}
  .cols {{ display: flex; align-items: stretch; }}
  .col {{ width: 50%; min-width: 0; }}
  .col + .col {{ border-left: 1px solid #e2e8f0; }}
  .col-head {{
    display: flex; justify-content: space-between; align-items: center;
    padding: 6px 14px; font-size: 11px; font-weight: 700;
    text-transform: uppercase; letter-spacing: .04em;
  }}
  .req .col-head {{ background: #eff6ff; color: #1d4ed8; border-bottom: 1px solid #dbeafe; }}
  .res .col-head {{ background: #ecfdf5; color: #047857; border-bottom: 1px solid #d1fae5; }}
  .elapsed {{ font-weight: 400; letter-spacing: 0; text-transform: none; color: #475569; }}
  pre {{
    margin: 0; padding: 12px 14px; white-space: pre-wrap; word-break: break-all;
    font: inherit;
  }}
  mark {{ background: #fde68a; color: #111827; padding: 0 1px; border-radius: 2px; }}
  .truncated {{ padding: 4px 14px 10px; color: #b91c1c; font-size: 11px; font-weight: 700; }}
  .foot {{ border-top: 1px solid #e2e8f0; background: #f8fafc; padding: 8px 16px; }}
  .verdict {{ display: flex; gap: 16px; flex-wrap: wrap; align-items: center; font-size: 12px; }}
  .verdict .badge {{ font-weight: 700; padding: 2px 8px; border-radius: 4px; }}
  .badge.match {{ background: #d1fae5; color: #065f46; }}
  .badge.mismatch {{ background: #fee2e2; color: #991b1b; }}
  .badge.inconclusive {{ background: #fef3c7; color: #92400e; }}
  .check {{ color: #334155; }}
  .check.bad {{ color: #b91c1c; font-weight: 700; }}
  .note {{ color: #92400e; font-size: 11px; margin-top: 4px; }}
  .caption {{ margin-top: 6px; color: #475569; font-size: 12px; }}
</style></head>
<body>
  <div class="frame">
    <div class="head">
      <div class="title">{_escape(title)}</div>
      <div class="meta">{_escape(meta)}</div>
    </div>
    <div class="cols">
      <div class="col req">
        <div class="col-head"><span>请求</span></div>
        <pre>{request_html}</pre>
      </div>
      <div class="col res">
        <div class="col-head"><span>响应</span>{elapsed}</div>
        <pre>{response_html}</pre>
        {truncation}
      </div>
    </div>
    <div class="foot">
      {verdict}
      {scheme_note}
      {caption}
    </div>
  </div>
</body></html>"""


def _verdict_html(comparison: Comparison) -> str:
    labels = {
        "match": "与上报一致",
        "mismatch": "与上报不一致",
        "inconclusive": "无法比对",
    }
    badge = labels.get(comparison.status, comparison.status)
    parts = [f'<span class="badge {comparison.status}">{badge}</span>']
    for check in comparison.checks:
        if check.ok is True:
            parts.append(f'<span class="check">{_escape(check.name)} ✓</span>')
        elif check.ok is False:
            parts.append(
                f'<span class="check bad">{_escape(check.name)} ✗'
                f"（期望 {_escape(check.expected)}，实际 {_escape(check.actual)}）</span>"
            )
    summary = f'<div class="verdict">{"".join(parts)}</div>'
    if comparison.notes:
        notes = "".join(f'<div class="note">{_escape(item)}</div>' for item in comparison.notes)
        summary += notes
    return summary


def resolve_chromium(explicit: str = "") -> str | None:
    """Find the browser, Linux first: the API runs on the VPS, not a laptop.

    The macOS branches exist so the same code path can be exercised locally, not
    because the service is expected to run there.
    """
    if explicit:
        path = Path(explicit).expanduser()
        # An explicit path that does not exist is a configuration error: falling back
        # to a different browser would hide the typo and render with something the
        # operator did not choose.
        return str(path) if path.is_file() else None

    roots = [
        Path.home() / ".cache/ms-playwright",
        Path.home() / "Library/Caches/ms-playwright",
    ]
    patterns = (
        "chromium-*/chrome-linux64/chrome",
        "chromium-*/chrome-linux/chrome",
        "chromium-*/chrome-mac*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
        "chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium",
    )
    for root in roots:
        if not root.is_dir():
            continue
        for pattern in patterns:
            found = sorted(root.glob(pattern), reverse=True)
            for candidate in found:
                if candidate.is_file():
                    return str(candidate)

    from shutil import which

    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = which(name)
        if found:
            return found
    # macOS app bundles: local development convenience only.
    for bundle in (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ):
        if Path(bundle).is_file():
            return bundle
    return None


def screenshot(payload: RenderInput, *, chromium: str = "") -> bytes:
    """Render to PNG. Raises :class:`RenderError` when chromium is unavailable."""
    binary = resolve_chromium(chromium)
    if not binary:
        raise RenderError(
            "未找到 chromium；请确认 ~/.cache/ms-playwright 下已安装，"
            "或设置 AXIOM_REPLAY_CHROME"
        )

    document = build_html(payload)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise RenderError(f"playwright 未安装：{exc}") from exc

    with _RENDER_LOCK:
        try:
            with sync_playwright() as driver:
                browser = driver.chromium.launch(
                    executable_path=binary,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
                try:
                    page = browser.new_page(
                        viewport={"width": RENDER_WIDTH, "height": 300},
                        device_scale_factor=DEVICE_SCALE,
                    )
                    page.set_content(document, wait_until="load")
                    page.wait_for_timeout(80)
                    # Size the viewport to the content before capturing: a
                    # full_page shot of a 900px-tall viewport leaves a large empty
                    # band under a two-line exchange, which looks like a bug in the
                    # report.  Measured in CSS pixels, so the 2x scale still applies.
                    height = page.evaluate(
                        "() => Math.ceil(document.querySelector('.frame').getBoundingClientRect().height)"
                        " + 40"
                    )
                    page.set_viewport_size(
                        {"width": RENDER_WIDTH, "height": max(160, min(4000, int(height)))}
                    )
                    page.wait_for_timeout(40)
                    return page.screenshot(full_page=True, type="png")
                finally:
                    browser.close()
        except Exception as exc:  # playwright raises its own error types
            raise RenderError(f"截图失败：{exc}") from exc


def render_input_for(
    *,
    packet_text: str,
    live: SendResult,
    comparison: Comparison | None,
    title: str,
    caption: str,
    highlights: list[str],
    source_label: str,
) -> RenderInput:
    """Assemble the payload from a live result, keeping the picture honest."""
    # The response box shows the live exchange, rebuilt from what the client
    # actually received — not the recorded text, which would let a stale capture
    # pose as this run's evidence.
    head = f"HTTP/1.1 {live.status_code or 0} {live.reason}".rstrip()
    header_lines = [f"{name}: {value}" for name, value in live.headers.items()]
    joined = "\n".join([head, *header_lines])
    response_text = f"{joined}\n\n{live.body}" if live.body else f"{joined}\n"

    scheme_note = ""
    if live.scheme_fell_back:
        scheme_note = f"https 不可用，实际使用 {live.scheme_used}"
    elif live.scheme_used:
        scheme_note = f"实际使用 {live.scheme_used}"

    return RenderInput(
        packet_text=packet_text,
        response_text=response_text,
        title=title,
        caption=caption,
        sent_at=_beijing_now(),
        source_label=source_label,
        elapsed_ms=live.elapsed_ms,
        highlights=highlights,
        comparison=comparison,
        body_truncated=live.body_truncated,
        original_body_bytes=live.body_bytes,
        scheme_note=scheme_note,
    )


__all__ = [
    "BEIJING",
    "RenderError",
    "RenderInput",
    "build_html",
    "render_input_for",
    "resolve_chromium",
    "screenshot",
]
