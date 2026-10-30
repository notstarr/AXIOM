"""Render a replay as the packet document a report needs.

Reports get submitted to clients, and a client reads a plain HTTP exchange far more
readily than a paragraph describing one.  The picture is generated from what was
actually sent and actually returned — never from text a worker supplied — so the
screenshot cannot show something the target did not do.

The layout is a two-column wire view: the request on the left, the response on the
right, each with line numbers and light syntax colouring.  That is the shape reviewers
already read in a proxy, and it survives being scaled into a Word document.  White
background on purpose: a delivered report is printed.

This module owns the only HTML in the replay path.  Callers supply data (packet text,
response text, highlight fragments, a caption); they never supply markup, which keeps
the renderer from becoming a general-purpose HTML-to-image service.
"""

from __future__ import annotations

import html
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from axiom.modules.replay.compare import Comparison


RENDER_WIDTH = 1440
DEVICE_SCALE = 2
SCREENSHOT_TIMEOUT_MS = 30_000

BEIJING = timezone(timedelta(hours=8))

# One chromium at a time.  The VPS has ~3.6G total and a single instance costs a few
# hundred MB; running two in parallel during an engagement is how the API process gets
# OOM-killed.
_RENDER_LOCK = threading.Lock()

_REQUEST_LINE = re.compile(r"^([A-Z]+)\s+(\S+)\s+(HTTP/\d(?:\.\d)?)$")
_STATUS_LINE = re.compile(r"^(HTTP/\d(?:\.\d)?)\s+(\d{3})(.*)$")
_HEADER_LINE = re.compile(r"^([!#$%&'*+\-.^_`|~0-9A-Za-z]+):\s?(.*)$")


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


def _highlight_fragments(text: str, fragments: list[str]) -> str:
    """Escape first, then wrap matches; fragments are matched on the escaped text."""
    escaped = _escape(text)
    for fragment in sorted({item for item in fragments if item}, key=len, reverse=True):
        needle = _escape(fragment)
        if not needle:
            continue
        escaped = escaped.replace(needle, f"<mark>{needle}</mark>")
    return escaped


def _split_packet(text: str) -> tuple[list[str], list[str]]:
    """Head lines and body lines, so each can be coloured for what it is."""
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
    if not normalized:
        return [], []
    head, _, body = normalized.partition("\n\n")
    head_lines = [line for line in head.split("\n")]
    body_lines = body.split("\n") if body else []
    return head_lines, body_lines


def _packet_rows(text: str, kind: str, highlights: list[str]) -> str:
    """Numbered rows.  ``kind`` decides which line of the head is the interesting one."""
    head_lines, body_lines = _split_packet(text)
    rows: list[str] = []
    number = 0

    def emit(markup: str, css: str = "") -> None:
        nonlocal number
        number += 1
        rows.append(
            f'<tr class="{css}"><td class="ln">{number}</td>'
            f'<td class="src">{markup}</td></tr>'
        )

    for index, line in enumerate(head_lines):
        cells: str
        if index == 0 and kind == "request":
            match = _REQUEST_LINE.match(line)
            if match:
                method, target, version = match.groups()
                cells = (
                    f'<span class="m">{_escape(method)}</span> '
                    f'{_highlight_fragments(target, highlights)} '
                    f'<span class="v">{_escape(version)}</span>'
                )
            else:
                cells = _highlight_fragments(line, highlights)
            emit(cells, "first")
        elif index == 0 and kind == "response":
            match = _STATUS_LINE.match(line)
            if match:
                version, code, reason = match.groups()
                tone = "ok" if code.startswith("2") else ("warn" if code.startswith("3") else "bad")
                cells = (
                    f'<span class="v">{_escape(version)}</span> '
                    f'<span class="s {tone}">{_escape(code)}</span>'
                    f'<span class="r">{_escape(reason)}</span>'
                )
            else:
                cells = _highlight_fragments(line, highlights)
            emit(cells, "first")
        else:
            match = _HEADER_LINE.match(line)
            if match:
                name, value = match.groups()
                cells = (
                    f'<span class="h">{_escape(name)}</span>'
                    f'<span class="c">:</span> '
                    f'{_highlight_fragments(value, highlights)}'
                )
            else:
                cells = _highlight_fragments(line, highlights)
            emit(cells, "hdr")
        if index == 0:
            pass

    if body_lines:
        # A blank row keeps the visual break between head and body without faking a line.
        rows.append('<tr class="gap"><td class="ln"></td><td class="src"></td></tr>')
        for line in body_lines:
            emit(_highlight_fragments(line, highlights), "body")

    return "".join(rows)


def build_html(payload: RenderInput) -> str:
    sent_at = payload.sent_at or _beijing_now()
    stamp = sent_at.astimezone(BEIJING).strftime("%Y-%m-%d %H:%M:%S")
    title = payload.title or "HTTP"

    meta_bits = [f"{stamp} (UTC+8)"]
    if payload.source_label:
        meta_bits.append(payload.source_label)
    if payload.elapsed_ms is not None:
        meta_bits.append(f"{payload.elapsed_ms} ms")
    meta = " · ".join(meta_bits)

    truncation = ""
    if payload.body_truncated:
        original_kb = max(1, round(payload.original_body_bytes / 1024))
        truncation = f'<div class="truncated">响应体已截断（原始 {original_kb} KB）</div>'

    scheme_note = ""
    if payload.scheme_note:
        scheme_note = f'<span class="note">{_escape(payload.scheme_note)}</span>'

    caption = ""
    if payload.caption:
        caption = f'<div class="caption">{_escape(payload.caption)}</div>'

    verdict = _verdict_html(payload.comparison) if payload.comparison is not None else ""

    request_rows = _packet_rows(payload.packet_text, "request", payload.highlights)
    response_rows = _packet_rows(payload.response_text, "response", payload.highlights)

    extra = "".join(part for part in (scheme_note, verdict, caption) if part)
    footer = f'<div class="foot">{extra}</div>' if extra else ""

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>{_escape(title)}</title>
<style>
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; background: #ffffff; }}
  body {{
    padding: 18px;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas,
                 "WenQuanYi Zen Hei", "PingFang SC", monospace;
    font-size: 12.5px; line-height: 1.62; color: #1e293b;
  }}
  .head {{
    display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap;
    padding: 0 2px 8px; border-bottom: 1px solid #e2e8f0;
  }}
  .head .title {{ font-weight: 700; font-size: 13.5px; color: #0f172a; }}
  .head .meta {{ color: #94a3b8; font-size: 11px; }}
  .cols {{ display: flex; align-items: stretch; }}
  .col {{ width: 50%; min-width: 0; }}
  .col + .col {{ border-left: 1px solid #e2e8f0; }}
  .col-head {{
    padding: 7px 12px; font-size: 10.5px; font-weight: 700; letter-spacing: .06em;
    text-transform: uppercase; color: #64748b; background: #f8fafc;
    border-bottom: 1px solid #e2e8f0;
  }}
  table.pk {{ width: 100%; border-collapse: collapse; table-layout: fixed; }}
  table.pk td {{ padding: 0 0 0 2px; vertical-align: top; }}
  td.ln {{
    width: 34px; padding: 0 9px 0 4px; text-align: right; color: #cbd5e1;
    font-size: 10px; user-select: none; vertical-align: top;
  }}
  td.src {{
    padding: 0 14px 0 8px; white-space: pre-wrap;
    word-break: break-all; color: #334155;
  }}
  tr.first td.src {{ color: #0f172a; }}
  tr.gap td {{ height: 8px; }}
  .m {{ color: #7c3aed; font-weight: 700; }}
  .v {{ color: #94a3b8; }}
  .s {{ font-weight: 700; }}
  .s.ok {{ color: #047857; }}
  .s.warn {{ color: #b45309; }}
  .s.bad {{ color: #be123c; }}
  .r {{ color: #475569; }}
  .h {{ color: #0f766e; }}
  .c {{ color: #94a3b8; }}
  mark {{ background: #fde68a; color: #111827; padding: 0 1px; border-radius: 2px; }}
  .truncated {{
    padding: 5px 12px; color: #b91c1c; font-weight: 700; font-size: 11px;
    border-top: 1px solid #fee2e2; background: #fef2f2;
  }}
  .foot {{ border-top: 1px solid #e2e8f0; padding: 8px 12px; }}
  .verdict {{ display: flex; gap: 14px; flex-wrap: wrap; align-items: center; font-size: 11.5px; }}
  .badge {{ font-weight: 700; padding: 1px 7px; border-radius: 3px; }}
  .badge.match {{ background: #d1fae5; color: #065f46; }}
  .badge.mismatch {{ background: #fee2e2; color: #991b1b; }}
  .badge.inconclusive {{ background: #fef3c7; color: #92400e; }}
  .check {{ color: #334155; }}
  .check.bad {{ color: #b91c1c; font-weight: 700; }}
  .note {{ color: #92400e; font-size: 11px; }}
  .caption {{ margin-top: 5px; color: #475569; font-size: 11.5px; }}
</style></head>
<body>
  <div class="head">
    <span class="title">{_escape(title)}</span>
    <span class="meta">{_escape(meta)}</span>
  </div>
  <div class="cols">
    <div class="col">
      <div class="col-head">请求</div>
      <table class="pk"><tbody>{request_rows}</tbody></table>
    </div>
    <div class="col">
      <div class="col-head">响应</div>
      <table class="pk"><tbody>{response_rows}</tbody></table>
      {truncation}
    </div>
  </div>
  {footer}
</body></html>"""


def _verdict_html(comparison: Comparison) -> str:
    labels = {"match": "与上报一致", "mismatch": "与上报不一致", "inconclusive": "无法比对"}
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
        summary += "".join(
            f'<div class="note">{_escape(item)}</div>' for item in comparison.notes
        )
    return summary


def resolve_chromium(explicit: str = "") -> str | None:
    """Find the browser, Linux first: the API runs on the VPS, not a laptop."""
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
            for candidate in sorted(root.glob(pattern), reverse=True):
                if candidate.is_file():
                    return str(candidate)

    from shutil import which

    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = which(name)
        if found:
            return found
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
                    # Size the viewport to the content before capturing: a full_page
                    # shot of a 900px-tall viewport leaves a large empty band under a
                    # two-line exchange, which looks like a bug in the report.
                    height = page.evaluate(
                        "() => Math.ceil(document.body.getBoundingClientRect().height) + 40"
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


def response_text_of(status_code: int | None, reason: str, headers: dict[str, str], body: str) -> str:
    """The response box shows what the client actually received, rebuilt in order."""
    head = f"HTTP/1.1 {status_code or 0} {reason}".rstrip()
    lines = [head]
    lines.extend(f"{name}: {value}" for name, value in headers.items())
    joined = "\n".join(lines)
    return f"{joined}\n\n{body}" if body else joined


__all__ = [
    "BEIJING",
    "DEVICE_SCALE",
    "RENDER_WIDTH",
    "RenderError",
    "RenderInput",
    "build_html",
    "resolve_chromium",
    "response_text_of",
    "screenshot",
]
