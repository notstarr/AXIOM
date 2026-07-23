#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cdp_ctl.py — Playwright-over-CDP CLI.

Connects to an already-running Chrome via CDP (connect_over_cdp), performs ONE
action per invocation, and exits. No daemon, no sessions, no persistent state.

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
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.request
from typing import Any

from playwright.sync_api import sync_playwright, Page, Browser, BrowserContext, Playwright


class CTLError(RuntimeError):
    pass


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


def _resolve_tab_url(cdp: str, tab_id: str) -> str:
    """Resolve a CDP target id to its current URL via the HTTP endpoint."""
    for p in _list_pages(cdp):
        if p.get("id") == tab_id:
            return p.get("url") or ""
    raise CTLError(f"tab {tab_id} not found in CDP target list")


# ---------------------------------------------------------------------------
# Playwright connection + page matching
# ---------------------------------------------------------------------------

def _connect(cdp: str) -> tuple[Playwright, Browser]:
    pw = sync_playwright().start()
    try:
        browser = pw.chromium.connect_over_cdp(cdp)
    except Exception as e:
        pw.stop()
        raise CTLError(f"connect_over_cdp failed: {e}")
    return pw, browser


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

    # Resolve tab_id → URL via CDP HTTP, then find the matching Playwright page.
    # tab_id might be a CDP target id or a URL substring.
    target_url = None
    try:
        target_url = _resolve_tab_url(cdp, tab_id)
    except CTLError:
        pass

    if target_url:
        for p in pages:
            if p.url == target_url:
                return p, browser
        # URL may have changed after navigation; try starts-with on origin.
        for p in pages:
            if target_url and p.url and (p.url.startswith(target_url[:30]) or target_url.startswith(p.url[:30])):
                return p, browser

    # Fallback: treat tab_id as a URL substring.
    for p in pages:
        if tab_id in (p.url or ""):
            return p, browser

    # Last resort: if tab_id resolved to a URL but page not found, the page
    # might be navigating. Return the first page.
    if pages:
        return pages[0], browser
    raise CTLError(f"could not find page for tab {tab_id}")


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
    el = elements[idx]
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
    print(f"HOVER_FAILED: element not found")
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
    try:
        page.locator("select").nth(idx).select_option(value=value, timeout=5000)
        _post_action(page, args.sleep)
        print(f"SELECTED [{idx}] value={value}")
        return 0
    except Exception as e:
        js = f"""
        (() => {{
          const sel = document.querySelectorAll('select')[{idx}];
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
            import re
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
    info = _new_tab(args.cdp, args.url)
    tid = info.get("id")
    if not tid:
        print("NEW_TAB_FAILED: no id returned")
        return 2
    print(f"TAB_ID: {tid}")
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
    _close_tab(args.cdp, args.target_id)
    print(f"CLOSED: {args.target_id}")
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
    sp = sub.add_parser("text"); sp.add_argument("--chars", type=int, default=4000)
    sp = sub.add_parser("eval"); sp.add_argument("js")
    sp = sub.add_parser("click"); sp.add_argument("target")
    sp = sub.add_parser("click-text"); sp.add_argument("target")
    sp = sub.add_parser("click-href"); sp.add_argument("target")
    sp = sub.add_parser("fill"); sp.add_argument("target"); sp.add_argument("value")
    sp = sub.add_parser("open"); sp.add_argument("url")
    sp = sub.add_parser("screenshot"); sp.add_argument("path")
    sp = sub.add_parser("full-screenshot"); sp.add_argument("path")
    sp = sub.add_parser("hover"); sp.add_argument("target")
    sp = sub.add_parser("select"); sp.add_argument("target"); sp.add_argument("value")
    sp = sub.add_parser("keys"); sp.add_argument("key")
    sp = sub.add_parser("scroll"); sp.add_argument("direction", choices=["down", "up"]); sp.add_argument("pixels", type=int, nargs="?", default=500)
    sp = sub.add_parser("back")
    sp = sub.add_parser("wait"); sp.add_argument("condition", choices=["text", "url"]); sp.add_argument("value")
    sp = sub.add_parser("cookies")
    sp = sub.add_parser("new-tab"); sp.add_argument("url")
    sp = sub.add_parser("close-tab"); sp.add_argument("target_id")

    args = p.parse_args(argv)

    # Commands that don't need a Playwright connection.
    if args.cmd == "new-tab":
        return cmd_new_tab(args)
    if args.cmd == "close-tab":
        return cmd_close_tab(args)

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
            "fill": cmd_fill, "open": cmd_open,
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
