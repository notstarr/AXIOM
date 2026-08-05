#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cdp_ctl.py — Playwright-over-CDP CLI.

Connects to an already-running Chrome via CDP (connect_over_cdp), performs ONE
action per invocation, and exits. Tabs created by AXIOM are registered by Lease
so the Runner can close them even when the model forgets.

Powered by Playwright's Page API: real mouse events, form fill, smart waits,
iframe / shadow-DOM support, dialog auto-dismiss.

Usage:
    python3 cdp_ctl.py [--cdp URL] [--tab-id ID|URL] <command> [args]

Commands:
    state               URL, title, readyState, indexed interactive elements.
    links               All links (index, text, href).
    url / title         Quick getters.
    text [--chars N]    document.body.innerText.
    eval "<js>"         Evaluate JS, print return value.
    click <idx>         Click element at state-index (real mouse event).
    click-text "<t>"    Click first element whose text contains <t>.
    click-href "<sub>"  Click first link whose href contains <sub>.
    hover <idx>         Hover element at state-index.
    fill <idx> "<val>"  Fill an input at state-index with a value.
    select <idx> "<v>"  Select an option in a <select> at state-index.
    keys "<key>"        Send keyboard event (Enter, Escape, Tab, etc).
    scroll down|up [px] Scroll page down or up (default 500px).
    back                Go back to previous page.
    open "<url>"        Navigate the tab, wait for networkidle, print new URL.
    wait text "<text>"  Wait until page contains text.
    wait url "<url>"    Wait until page URL matches.
    cookies             Print all cookies as JSON.
    screenshot <path>   Save a viewport screenshot.
    full-screenshot <path>  Save a full-page screenshot.
    new-tab "<url>"     Create a dedicated tab, return its CDP target id.
    close-tab <id>      Close a tab by CDP target id.
    close-lease-tabs <lease>  Close all AXIOM tabs registered to one Lease.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, Page, Browser, BrowserContext, Playwright


class CTLError(RuntimeError):
    pass


DEFAULT_TAB_TTL_SECONDS = 2 * 60 * 60


# ---------------------------------------------------------------------------
# CDP HTTP helpers (tab lifecycle only — Playwright handles the rest)
# ---------------------------------------------------------------------------

def _http_json(url: str, timeout: float = 5.0) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _list_pages(cdp: str) -> list[dict]:
    return [p for p in _http_json(cdp.rstrip("/") + "/json/list") if p.get("type") == "page"]


def _new_tab(cdp: str, url: str) -> dict:
    from urllib.parse import quote
    req = urllib.request.Request(cdp.rstrip("/") + "/json/new?" + quote(url, safe=""), method="PUT")
    with urllib.request.urlopen(req, timeout=10.0) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _close_tab(cdp: str, target_id: str) -> None:
    try:
        urllib.request.urlopen(cdp.rstrip("/") + f"/json/close/{target_id}", timeout=5.0)
    except Exception:
        pass


def _cdp_port(cdp: str) -> int:
    parsed = urlparse(cdp)
    if parsed.port:
        return parsed.port
    return 443 if parsed.scheme == "https" else 80


def _registry_path(cdp: str) -> Path:
    configured = os.environ.get("AXIOM_CDP_TAB_REGISTRY", "").strip()
    if configured:
        return Path(configured.replace("{port}", str(_cdp_port(cdp)))).expanduser()
    return Path(f"/tmp/axiom-cdp-tabs-{_cdp_port(cdp)}.json")


@contextlib.contextmanager
def _registry_lock(path: Path):
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _load_registry(path: Path) -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    tabs = payload.get("tabs") if isinstance(payload, dict) else None
    return tabs if isinstance(tabs, dict) else {}


def _write_registry(path: Path, tabs: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        temporary.write_text(
            json.dumps({"version": 1, "tabs": tabs}, ensure_ascii=False),
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _mutate_registry(cdp: str, callback) -> Any:
    path = _registry_path(cdp)
    with _registry_lock(path):
        tabs = _load_registry(path)
        result = callback(tabs)
        _write_registry(path, tabs)
        return result


def _register_tab(cdp: str, target_id: str, alias: str = "") -> None:
    now = time.time()
    lease_id = os.environ.get("AXIOM_LEASE_ID", "").strip()
    intent_id = os.environ.get("AXIOM_INTENT_ID", "").strip()
    task_id = os.environ.get("AXIOM_TASK_ID", "").strip()

    def mutate(tabs):
        tabs[target_id] = {
            "target_id": target_id,
            "alias": alias,
            "lease_id": lease_id,
            "task_id": task_id,
            "intent_id": intent_id,
            "created_at": now,
            "updated_at": now,
        }

    _mutate_registry(cdp, mutate)


def _unregister_tab(cdp: str, target_id: str) -> None:
    _mutate_registry(cdp, lambda tabs: tabs.pop(target_id, None))


def _registered_target(
    cdp: str, identifier: str, *, current_lease_only: bool = False
) -> str | None:
    path = _registry_path(cdp)
    with _registry_lock(path):
        tabs = _load_registry(path)
    lease_id = os.environ.get("AXIOM_LEASE_ID", "").strip()
    candidates = [
        record
        for record in tabs.values()
        if isinstance(record, dict)
        and (
            not current_lease_only
            or not lease_id
            or str(record.get("lease_id") or "") == lease_id
        )
        and identifier
        in {
            str(record.get("target_id") or ""),
            str(record.get("alias") or ""),
            str(record.get("intent_id") or ""),
        }
    ]
    candidates.sort(
        key=lambda record: (
            str(record.get("lease_id") or "") == lease_id,
            float(record.get("updated_at") or 0),
        ),
        reverse=True,
    )
    return str(candidates[0].get("target_id") or "") if candidates else None


def _touch_tab(cdp: str, target_id: str) -> None:
    def mutate(tabs):
        record = tabs.get(target_id)
        if isinstance(record, dict):
            record["updated_at"] = time.time()

    _mutate_registry(cdp, mutate)


def _resolve_tab_info(cdp: str, tab_id: str | None) -> dict:
    pages = _list_pages(cdp)
    if not pages:
        raise CTLError("no pages available")
    lease_id = os.environ.get("AXIOM_LEASE_ID", "").strip()
    if lease_id and not tab_id:
        raise CTLError(
            "--tab-id is required inside an AXIOM Lease; refusing to select another worker's page"
        )
    if tab_id:
        registered = _registered_target(
            cdp,
            tab_id,
            current_lease_only=bool(lease_id),
        )
        if lease_id and not registered:
            raise CTLError(
                f"tab {tab_id} is not registered to the current AXIOM Lease"
            )
        target_ids = {registered or tab_id}
        for page in pages:
            if page.get("id") in target_ids:
                _touch_tab(cdp, str(page.get("id") or ""))
                return page
        raise CTLError(f"tab {tab_id} not found in CDP target list")

    for page in pages:
        url = str(page.get("url") or "")
        if url and not url.startswith(("about:blank", "chrome")):
            return page
    return pages[0]


def _cleanup_registered_tabs(
    cdp: str,
    *,
    lease_id: str | None = None,
    older_than_seconds: float | None = None,
) -> list[str]:
    now = time.time()
    try:
        live_ids = {str(page.get("id") or "") for page in _list_pages(cdp)}
    except Exception:
        live_ids = set()
    path = _registry_path(cdp)
    with _registry_lock(path):
        tabs = _load_registry(path)
        close_ids: list[str] = []
        for target_id, record in list(tabs.items()):
            if target_id not in live_ids:
                tabs.pop(target_id, None)
                continue
            if lease_id is not None:
                should_close = str(record.get("lease_id") or "") == lease_id
            elif older_than_seconds is not None:
                updated_at = float(record.get("updated_at") or 0)
                should_close = now - updated_at >= older_than_seconds
            else:
                should_close = False
            if should_close:
                close_ids.append(target_id)
                tabs.pop(target_id, None)
        _write_registry(path, tabs)
    for target_id in close_ids:
        _close_tab(cdp, target_id)
    return close_ids


def _cleanup_stale_tabs(cdp: str) -> list[str]:
    raw = os.environ.get("AXIOM_BROWSER_TAB_TTL_SECONDS", "").strip()
    try:
        ttl = float(raw) if raw else DEFAULT_TAB_TTL_SECONDS
    except ValueError:
        ttl = DEFAULT_TAB_TTL_SECONDS
    return _cleanup_registered_tabs(cdp, older_than_seconds=max(ttl, 60.0))


def _resolve_tab_url(cdp: str, tab_id: str) -> str:
    """Resolve a CDP target id to its current URL via the HTTP endpoint."""
    return str(_resolve_tab_info(cdp, tab_id).get("url") or "")


# ---------------------------------------------------------------------------
# Playwright connection + page matching
# ---------------------------------------------------------------------------

def _connect(cdp: str) -> tuple[Playwright, Browser]:
    pw = sync_playwright().start()
    try:
        raw_timeout = os.environ.get("CDP_CONNECT_TIMEOUT_MS", "10000")
        try:
            timeout_ms = max(1000, int(raw_timeout))
        except ValueError:
            timeout_ms = 10000
        browser = pw.chromium.connect_over_cdp(cdp, timeout=timeout_ms)
    except Exception as e:
        pw.stop()
        raise CTLError(f"connect_over_cdp failed: {e}")
    return pw, browser


def _page_target_id(context: BrowserContext, page: Page) -> str:
    session = context.new_cdp_session(page)
    try:
        payload = session.send("Target.getTargetInfo")
        return str((payload.get("targetInfo") or {}).get("targetId") or "")
    finally:
        try:
            session.detach()
        except Exception:
            pass


def _find_page(browser: Browser, tab_id: str | None, cdp: str) -> tuple[Page, Browser]:
    """Find the Playwright Page object for our dedicated tab."""
    contexts = browser.contexts
    if not contexts:
        raise CTLError("no browser contexts available")
    ctx: BrowserContext = contexts[0]
    pages = ctx.pages

    if tab_id is None:
        # Pick the first non-blank page.
        for p in pages:
            u = p.url or ""
            if u and not u.startswith("about:blank") and not u.startswith("chrome"):
                return p, browser
        if pages:
            return pages[0], browser
        raise CTLError("no pages available")

    target = _resolve_tab_info(cdp, tab_id)
    target_id = str(target.get("id") or "")
    for page in pages:
        try:
            if _page_target_id(ctx, page) == target_id:
                return page, browser
        except Exception:
            continue

    # CDP attachment can race with a navigation. URL fallback is acceptable
    # only when it identifies exactly one page; never silently use page zero.
    target_url = str(target.get("url") or "")
    matches = [page for page in pages if target_url and page.url == target_url]
    if len(matches) == 1:
        return matches[0], browser
    raise CTLError(f"could not find Playwright page for CDP target {target_id}")


# ---------------------------------------------------------------------------
# Interactive element snapshot (injected via Playwright eval)
# ---------------------------------------------------------------------------

_SNAP_JS = r"""
(() => {
  const sel = 'a, button, input:not([type=hidden]), select, textarea, [role="button"], [onclick]';
  const nodes = Array.from(document.querySelectorAll(sel));
  const visible = nodes.filter(el => {
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return false;
    const s = getComputedStyle(el);
    if (s.visibility === 'hidden' || s.display === 'none' || Number(s.opacity) === 0) return false;
    return true;
  });
  return visible.map((el, i) => {
    const tag = el.tagName.toLowerCase();
    const text = (el.innerText || el.value || el.getAttribute('aria-label') || el.title || '').replace(/\s+/g, ' ').trim().slice(0, 100);
    const href = el.getAttribute('href') || '';
    const role = el.getAttribute('role') || '';
    const type = el.getAttribute('type') || '';
    const name = el.getAttribute('name') || '';
    return {i, tag, text, href, role, type, name};
  });
})()
"""


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def cmd_state(page: Page, args) -> int:
    url = page.url or ""
    title = page.title() or ""
    ready = page.evaluate("document.readyState") or ""
    print(f"URL: {url}")
    print(f"TITLE: {title}")
    print(f"READY: {ready}")
    elements = page.evaluate(_SNAP_JS) or []
    print(f"ELEMENTS: {len(elements)}")
    for el in elements:
        label = el.get("text", "")
        href = el.get("href", "")
        tag = el.get("tag", "")
        role = el.get("role", "")
        extras = []
        if href:
            extras.append(f"href={href[:100]}")
        if role:
            extras.append(f"role={role}")
        if el.get("type"):
            extras.append(f"type={el['type']}")
        if el.get("name"):
            extras.append(f"name={el['name']}")
        extra = " " + " ".join(extras) if extras else ""
        print(f"[{el['i']}] {tag} {label!r}{extra}")
    return 0


def cmd_links(page: Page, args) -> int:
    rows = page.evaluate(
        "Array.from(document.querySelectorAll('a')).map((a,i)=>({i:i, text:(a.innerText||'').replace(/\\s+/g,' ').trim().slice(0,80), href:a.getAttribute('href')||''}))"
    ) or []
    for r in rows:
        print(f"[{r['i']}] {r.get('text','')!r} -> {r.get('href','')}")
    return 0


def cmd_url(page: Page, args) -> int:
    print(page.url or "")
    return 0


def cmd_title(page: Page, args) -> int:
    print(page.title() or "")
    return 0


def cmd_text(page: Page, args) -> int:
    n = args.chars
    txt = page.evaluate("(document.body&&document.body.innerText)||''") or ""
    print(txt[:n])
    return 0


def cmd_eval(page: Page, args) -> int:
    val = page.evaluate(args.js)
    if val is None:
        print("undefined")
    elif isinstance(val, (dict, list)):
        print(json.dumps(val, ensure_ascii=False))
    else:
        print(val)
    return 0


def _get_visible_elements(page: Page) -> list:
    return page.evaluate(_SNAP_JS) or []


def cmd_click(page: Page, args) -> int:
    idx = int(args.target)
    elements = _get_visible_elements(page)
    if idx < 0 or idx >= len(elements):
        print(f"CLICK_FAILED: index {idx} out of range (visible={len(elements)})")
        return 2
    el = elements[idx]
    # Use Playwright's locator approach: find by the same selector logic, then click.
    _click_by_index(page, idx)
    _post_action(page, args.sleep)
    print(f"CLICKED [{idx}] {el.get('tag','')} {el.get('text','') or el.get('href','')}")
    return 0


def cmd_click_text(page: Page, args) -> int:
    needle = args.target
    # Try Playwright's native get_by_text first (handles shadow DOM, aria, etc.)
    try:
        locator = page.get_by_text(needle, exact=False).first
        locator.click(timeout=5000)
        _post_action(page, args.sleep)
        print(f"CLICKED (text) {needle}")
        return 0
    except Exception:
        pass
    # Fallback: JS-based click
    js = f"""
    (() => {{
      const sel = 'a, button, input:not([type=hidden]), select, textarea, [role="button"], [onclick]';
      const nodes = Array.from(document.querySelectorAll(sel));
      const visible = nodes.filter(el => {{
        const r = el.getBoundingClientRect();
        if (r.width === 0 && r.height === 0) return false;
        const s = getComputedStyle(el);
        if (s.visibility === 'hidden' || s.display === 'none' || Number(s.opacity) === 0) return false;
        return true;
      }});
      const nl = {json.dumps(needle, ensure_ascii=False)}.toLowerCase();
      for (const el of visible) {{
        const t = (el.innerText || el.value || el.getAttribute('aria-label') || el.title || '').toLowerCase();
        if (t && t.includes(nl)) {{
          el.scrollIntoView({{block: 'center', inline: 'center'}});
          el.click();
          return {{ok: true, clicked: el.tagName.toLowerCase(), text: (el.innerText||el.value||'').slice(0,80)}};
        }}
      }}
      return {{ok: false, error: 'no visible element text contains: ' + {json.dumps(needle, ensure_ascii=False)}}};
    }})()
    """
    res = page.evaluate(js)
    if isinstance(res, dict) and res.get("ok"):
        _post_action(page, args.sleep)
        print(f"CLICKED (text-js) {res.get('clicked','')} {res.get('text','')}")
        return 0
    print(f"CLICK_FAILED: {(res or {}).get('error') if isinstance(res, dict) else res}")
    return 2


def cmd_click_href(page: Page, args) -> int:
    sub = args.target
    js = f"""
    (() => {{
      const links = Array.from(document.querySelectorAll('a'));
      const visible = links.filter(el => {{
        const r = el.getBoundingClientRect();
        if (r.width === 0 && r.height === 0) return false;
        const s = getComputedStyle(el);
        if (s.visibility === 'hidden' || s.display === 'none' || Number(s.opacity) === 0) return false;
        return true;
      }});
      for (const el of visible) {{
        const href = el.getAttribute('href') || '';
        if (href && href.includes({json.dumps(sub, ensure_ascii=False)})) {{
          el.scrollIntoView({{block: 'center', inline: 'center'}});
          el.click();
          return {{ok: true, clicked: 'a', href: href.slice(0,120), text: (el.innerText||'').slice(0,80)}};
        }}
      }}
      return {{ok: false, error: 'no visible link href contains: ' + {json.dumps(sub, ensure_ascii=False)}}};
    }})()
    """
    res = page.evaluate(js)
    if isinstance(res, dict) and res.get("ok"):
        _post_action(page, args.sleep)
        print(f"CLICKED (href) {res.get('href','')}")
        return 0
    print(f"CLICK_FAILED: {(res or {}).get('error') if isinstance(res, dict) else res}")
    return 2


def cmd_fill(page: Page, args) -> int:
    """Fill an input element at state-index with a value, then dispatch input/change."""
    idx = int(args.target)
    value = args.value
    elements = _get_visible_elements(page)
    if idx < 0 or idx >= len(elements):
        print(f"FILL_FAILED: index {idx} out of range (visible={len(elements)})")
        return 2
    # Use Playwright's eval to set value and dispatch events (React-compatible).
    js = f"""
    (() => {{
      const sel = 'a, button, input:not([type=hidden]), select, textarea, [role="button"], [onclick]';
      const nodes = Array.from(document.querySelectorAll(sel));
      const visible = nodes.filter(el => {{
        const r = el.getBoundingClientRect();
        if (r.width === 0 && r.height === 0) return false;
        const s = getComputedStyle(el);
        if (s.visibility === 'hidden' || s.display === 'none' || Number(s.opacity) === 0) return false;
        return true;
      }});
      const el = visible[{idx}];
      if (!el) return {{ok: false, error: 'element not found'}};
      el.scrollIntoView({{block: 'center'}});
      el.focus();
      const setter = Object.getOwnPropertyDescriptor(el.__proto__, 'value')?.set;
      if (setter) setter.call(el, {json.dumps(value, ensure_ascii=False)});
      else el.value = {json.dumps(value, ensure_ascii=False)};
      el.dispatchEvent(new Event('input', {{bubbles: true}}));
      el.dispatchEvent(new Event('change', {{bubbles: true}}));
      return {{ok: true, tag: el.tagName.toLowerCase(), name: el.getAttribute('name')||'', value: el.value.slice(0,80)}};
    }})()
    """
    res = page.evaluate(js)
    if isinstance(res, dict) and res.get("ok"):
        print(f"FILLED [{idx}] {res.get('tag','')} name={res.get('name','')} value={res.get('value','')}")
        return 0
    print(f"FILL_FAILED: {(res or {}).get('error') if isinstance(res, dict) else res}")
    return 2


def cmd_open(page: Page, args) -> int:
    try:
        page.goto(args.url, wait_until="domcontentloaded", timeout=15000)
    except Exception as e:
        print(f"NAVIGATE_NOTE: {e} (continuing)")
    # Wait for network to settle (best-effort).
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    print(f"URL: {page.url or ''}")
    print(f"TITLE: {page.title() or ''}")
    try:
        print(f"READY: {page.evaluate('document.readyState') or ''}")
    except Exception:
        print("READY: unknown")
    return 0


def cmd_screenshot(page: Page, args) -> int:
    out = args.path
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    try:
        page.screenshot(path=out, full_page=False)
        sz = os.path.getsize(out)
        print(f"SCREENSHOT_SAVED: {out} ({sz} bytes)")
    except Exception as e:
        print(f"SCREENSHOT_FAILED: {e}")
        return 2
    return 0


def cmd_full_screenshot(page: Page, args) -> int:
    out = args.path
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    try:
        page.screenshot(path=out, full_page=True)
        sz = os.path.getsize(out)
        print(f"FULL_SCREENSHOT_SAVED: {out} ({sz} bytes)")
    except Exception as e:
        print(f"SCREENSHOT_FAILED: {e}")
        return 2
    return 0


def cmd_hover(page: Page, args) -> int:
    idx = int(args.target)
    elements = _get_visible_elements(page)
    if idx < 0 or idx >= len(elements):
        print(f"HOVER_FAILED: index {idx} out of range (visible={len(elements)})")
        return 2
    js = f"""
    (() => {{
      const sel = 'a, button, input:not([type=hidden]), select, textarea, [role="button"], [onclick]';
      const nodes = Array.from(document.querySelectorAll(sel));
      const visible = nodes.filter(el => {{
        const r = el.getBoundingClientRect();
        if (r.width === 0 && r.height === 0) return false;
        const s = getComputedStyle(el);
        if (s.visibility === 'hidden' || s.display === 'none' || Number(s.opacity) === 0) return false;
        return true;
      }});
      const el = visible[{idx}];
      if (!el) return null;
      el.scrollIntoView({{block: 'center', inline: 'center'}});
      const r = el.getBoundingClientRect();
      return {{x: r.x + r.width/2, y: r.y + r.height/2}};
    }})()
    """
    box = page.evaluate(js)
    if box and isinstance(box, dict):
        page.mouse.move(box["x"], box["y"])
        _post_action(page, args.sleep)
        print(f"HOVERED [{idx}]")
        return 0
    print("HOVER_FAILED: element not found")
    return 2


def cmd_select(page: Page, args) -> int:
    idx = int(args.target)
    value = args.value
    elements = _get_visible_elements(page)
    if idx < 0 or idx >= len(elements):
        print(f"SELECT_FAILED: index {idx} out of range (visible={len(elements)})")
        return 2
    el = elements[idx]
    if el.get("tag") != "select":
        print(f"SELECT_FAILED: element at [{idx}] is not a <select> (got {el.get('tag')})")
        return 2
    handle = None
    try:
        handle = page.evaluate_handle(
            """index => {
              const all = Array.from(document.querySelectorAll(
                'a, button, input:not([type=hidden]), select, textarea, [role="button"], [onclick]'
              )).filter(el => {
                const r = el.getBoundingClientRect();
                if (r.width === 0 && r.height === 0) return false;
                const s = getComputedStyle(el);
                return s.visibility !== 'hidden' && s.display !== 'none' && Number(s.opacity) !== 0;
              });
              return all[index] || null;
            }""",
            idx,
        ).as_element()
        if handle is None:
            raise RuntimeError("visible select element not found")
        handle.select_option(value=value, timeout=5000)
        _post_action(page, args.sleep)
        print(f"SELECTED [{idx}] value={value}")
        return 0
    except Exception as e:
        js = f"""
        (() => {{
          const all = Array.from(document.querySelectorAll(
            'a, button, input:not([type=hidden]), select, textarea, [role="button"], [onclick]'
          )).filter(el => {{
            const r = el.getBoundingClientRect();
            if (r.width === 0 && r.height === 0) return false;
            const s = getComputedStyle(el);
            return s.visibility !== 'hidden' && s.display !== 'none' && Number(s.opacity) !== 0;
          }});
          const sel = all[{idx}];
          if (!sel) return {{ok: false}};
          sel.value = {json.dumps(value, ensure_ascii=False)};
          sel.dispatchEvent(new Event('change', {{bubbles: true}}));
          return {{ok: true, value: sel.value}};
        }})()
        """
        res = page.evaluate(js)
        if isinstance(res, dict) and res.get("ok"):
            print(f"SELECTED [{idx}] value={res.get('value')}")
            return 0
        print(f"SELECT_FAILED: {e}")
        return 2
    finally:
        if handle is not None:
            handle.dispose()


def cmd_keys(page: Page, args) -> int:
    key = args.key
    try:
        page.keyboard.press(key)
        _post_action(page, args.sleep)
        print(f"KEYS: {key}")
        return 0
    except Exception as e:
        print(f"KEYS_FAILED: {e}")
        return 2


def cmd_scroll(page: Page, args) -> int:
    direction = args.direction
    pixels = args.pixels
    delta = pixels if direction == "down" else -pixels
    page.mouse.wheel(0, delta)
    time.sleep(0.5)
    print(f"SCROLLED {direction} {pixels}px")
    return 0


def cmd_back(page: Page, args) -> int:
    try:
        page.go_back(timeout=10000)
        _post_action(page, args.sleep)
        print(f"URL: {page.url or ''}")
        return 0
    except Exception as e:
        print(f"BACK_FAILED: {e}")
        return 2


def cmd_wait(page: Page, args) -> int:
    condition = args.condition
    value = args.value
    try:
        if condition == "text":
            page.wait_for_selector(f"text={value}", timeout=15000)
            print(f"WAIT_OK: text '{value}' found")
        elif condition == "url":
            page.wait_for_url(f"**{value}**", timeout=15000)
            print(f"WAIT_OK: url contains '{value}'")
        else:
            print(f"WAIT_FAILED: unknown condition '{condition}'")
            return 2
        return 0
    except Exception as e:
        print(f"WAIT_TIMEOUT: {e}")
        return 1


def cmd_cookies(page: Page, args) -> int:
    context = page.context
    cookies = context.cookies()
    print(json.dumps(cookies, ensure_ascii=False, indent=2))
    return 0


def _click_by_index(page: Page, idx: int) -> None:
    """Click an element by its visible-index using Playwright's mouse for real events."""
    # Get the element's bounding box, then click at its center with Playwright mouse.
    js = f"""
    (() => {{
      const sel = 'a, button, input:not([type=hidden]), select, textarea, [role="button"], [onclick]';
      const nodes = Array.from(document.querySelectorAll(sel));
      const visible = nodes.filter(el => {{
        const r = el.getBoundingClientRect();
        if (r.width === 0 && r.height === 0) return false;
        const s = getComputedStyle(el);
        if (s.visibility === 'hidden' || s.display === 'none' || Number(s.opacity) === 0) return false;
        return true;
      }});
      const el = visible[{idx}];
      if (!el) return null;
      el.scrollIntoView({{block: 'center', inline: 'center'}});
      const r = el.getBoundingClientRect();
      return {{x: r.x + r.width/2, y: r.y + r.height/2, tag: el.tagName.toLowerCase()}};
    }})()
    """
    box = page.evaluate(js)
    if box and isinstance(box, dict):
        page.mouse.click(box["x"], box["y"])
    else:
        raise CTLError(f"element at index {idx} not found")


def _post_action(page: Page, sleep: float) -> None:
    """Wait after a click/navigation for things to settle."""
    time.sleep(max(0.3, sleep * 0.5))
    try:
        page.wait_for_load_state("domcontentloaded", timeout=5000)
    except Exception:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Tab lifecycle (no Playwright needed)
# ---------------------------------------------------------------------------

def cmd_new_tab(args) -> int:
    _cleanup_stale_tabs(args.cdp)
    alias = (
        (args.tab_id or "").strip()
        or os.environ.get("AXIOM_INTENT_ID", "").strip()
        or os.environ.get("AXIOM_LEASE_ID", "").strip()
    )
    if alias:
        existing = _registered_target(
            args.cdp, alias, current_lease_only=True
        )
        if existing:
            _close_tab(args.cdp, existing)
            _unregister_tab(args.cdp, existing)
    info = _new_tab(args.cdp, args.url)
    tid = info.get("id")
    if not tid:
        print("NEW_TAB_FAILED: no id returned")
        return 2
    _register_tab(args.cdp, str(tid), alias)
    print(f"TAB_ID: {tid}")
    if alias:
        print(f"TAB_ALIAS: {alias}")
    # Wait for the new tab to load.
    time.sleep(2)
    try:
        for p in _list_pages(args.cdp):
            if p.get("id") == tid:
                print(f"URL: {p.get('url','')}")
                print(f"TITLE: {p.get('title','')}")
                break
    except Exception:
        pass
    return 0


def cmd_close_tab(args) -> int:
    lease_scoped = bool(os.environ.get("AXIOM_LEASE_ID", "").strip())
    registered = _registered_target(
        args.cdp,
        args.target_id,
        current_lease_only=lease_scoped,
    )
    if lease_scoped and not registered:
        print("CLOSE_TAB_FAILED: tab is not registered to the current AXIOM Lease")
        return 3
    target_id = registered or args.target_id
    _close_tab(args.cdp, target_id)
    _unregister_tab(args.cdp, target_id)
    print(f"CLOSED: {target_id}")
    return 0


def cmd_close_lease_tabs(args) -> int:
    closed = _cleanup_registered_tabs(args.cdp, lease_id=args.lease_id)
    print(f"CLOSED_LEASE_TABS: {len(closed)}")
    for target_id in closed:
        print(target_id)
    return 0


def cmd_cleanup_tabs(args) -> int:
    closed = _cleanup_registered_tabs(
        args.cdp,
        older_than_seconds=max(float(args.older_than), 0.0),
    )
    print(f"CLOSED_STALE_TABS: {len(closed)}")
    return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Playwright-over-CDP CLI for the crawler")
    p.add_argument("--cdp", default=os.environ.get("CDP_URL", "http://localhost:9223"),
                   help="CDP HTTP endpoint (default http://localhost:9223)")
    p.add_argument("--tab-id", dest="tab_id", default=os.environ.get("CDP_TAB_ID"),
                   help="CDP target id or URL substring to identify the tab")
    p.add_argument("--sleep", type=float, default=1.5,
                   help="seconds to wait after click/open for navigation (default 1.5)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("state")
    sub.add_parser("links")
    sub.add_parser("url")
    sub.add_parser("title")
    sp = sub.add_parser("text")
    sp.add_argument("--chars", type=int, default=4000)
    sp = sub.add_parser("eval")
    sp.add_argument("js")
    sp = sub.add_parser("click")
    sp.add_argument("target")
    sp = sub.add_parser("click-text")
    sp.add_argument("target")
    sp = sub.add_parser("click-href")
    sp.add_argument("target")
    sp = sub.add_parser("fill")
    sp.add_argument("target")
    sp.add_argument("value")
    sp = sub.add_parser("open")
    sp.add_argument("url")
    sp = sub.add_parser("screenshot")
    sp.add_argument("path")
    sp = sub.add_parser("full-screenshot")
    sp.add_argument("path")
    sp = sub.add_parser("hover")
    sp.add_argument("target")
    sp = sub.add_parser("select")
    sp.add_argument("target")
    sp.add_argument("value")
    sp = sub.add_parser("keys")
    sp.add_argument("key")
    sp = sub.add_parser("scroll")
    sp.add_argument("direction", choices=["down", "up"])
    sp.add_argument("pixels", type=int, nargs="?", default=500)
    sp = sub.add_parser("back")
    sp = sub.add_parser("wait")
    sp.add_argument("condition", choices=["text", "url"])
    sp.add_argument("value")
    sp = sub.add_parser("cookies")
    sp = sub.add_parser("new-tab")
    sp.add_argument("url")
    sp = sub.add_parser("close-tab")
    sp.add_argument("target_id")
    sp = sub.add_parser("close-lease-tabs")
    sp.add_argument("lease_id")
    sp = sub.add_parser("cleanup-tabs")
    sp.add_argument(
        "--older-than", type=float, default=DEFAULT_TAB_TTL_SECONDS
    )

    args = p.parse_args(argv)

    # Commands that don't need a Playwright connection.
    if args.cmd == "new-tab":
        return cmd_new_tab(args)
    if args.cmd == "close-tab":
        return cmd_close_tab(args)
    if args.cmd == "close-lease-tabs":
        return cmd_close_lease_tabs(args)
    if args.cmd == "cleanup-tabs":
        return cmd_cleanup_tabs(args)
    if args.cmd in {"url", "title"}:
        try:
            info = _resolve_tab_info(args.cdp, args.tab_id)
        except CTLError as exc:
            print(f"CDP_ERROR: {exc}")
            return 3
        print(info.get(args.cmd, "") or "")
        return 0

    _cleanup_stale_tabs(args.cdp)

    # Connect to Chrome via CDP.
    try:
        pw, browser = _connect(args.cdp)
    except CTLError as e:
        print(f"CDP_ERROR: {e}")
        return 3
    except Exception as e:
        print(f"CDP_ERROR: connect failed: {e}")
        return 3

    try:
        page, _ = _find_page(browser, args.tab_id, args.cdp)

        # Auto-dismiss JS dialogs.
        page.on("dialog", lambda d: d.dismiss())
        page.on("console", lambda m: None)  # suppress console noise

        handlers = {
            "state": cmd_state, "links": cmd_links, "url": cmd_url, "title": cmd_title,
            "text": cmd_text, "eval": cmd_eval, "click": cmd_click,
            "click-text": cmd_click_text, "click-href": cmd_click_href,
            "hover": cmd_hover, "fill": cmd_fill, "select": cmd_select,
            "keys": cmd_keys, "scroll": cmd_scroll, "back": cmd_back,
            "wait": cmd_wait, "cookies": cmd_cookies,
            "open": cmd_open,
            "screenshot": cmd_screenshot, "full-screenshot": cmd_full_screenshot,
        }
        fn = handlers[args.cmd]
        return fn(page, args)
    except CTLError as e:
        print(f"CDP_ERROR: {e}")
        return 3
    except Exception as e:
        import traceback
        print(f"ERROR: {e}")
        traceback.print_exc()
        return 1
    finally:
        try:
            # IMPORTANT: when connected over CDP, we only detach Playwright.
            # Do NOT close the remote browser process, otherwise user tabs may be closed.
            pw.stop()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
