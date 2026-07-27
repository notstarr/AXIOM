#!/usr/bin/env python3
"""
XSS Verify — CDP dialog detector with multi-worker isolation.

Creates a NEW tab in the Chrome-SecTest instance, navigates to the victim URL,
waits for alert() dialog, then closes the tab. This ensures multiple workers
running XSS verification simultaneously do not interfere with each other.

Exit codes:
  0 = dialog fired, message printed to stdout   → XSS CONFIRMED
  1 = timeout, no dialog                        → XSS NOT triggered
  2 = error (Chrome not running, CDP error)

Usage:
    python3 detect.py <URL> [--timeout 8] [--cdp-port 9223] [--tab-index 0]

Multi-worker isolation:
    By default, creates a new tab (--new-tab). Use --tab-index N to reuse
    an existing tab (NOT recommended when other workers may be running).
"""
import sys
import json
import time
import argparse
import threading
import urllib.request
from urllib.parse import quote

try:
    import websocket
except ImportError:
    print("ERROR: pip3 install websocket-client", file=sys.stderr)
    sys.exit(2)


def get_tabs(port):
    try:
        raw = urllib.request.urlopen(f"http://localhost:{port}/json/list", timeout=3).read()
        return json.loads(raw)
    except Exception as e:
        print(f"ERROR: Chrome not responding on port {port}: {e}", file=sys.stderr)
        sys.exit(2)


def create_new_tab(port, url):
    """Create a new tab via CDP HTTP API and return its WebSocket URL."""
    try:
        request = urllib.request.Request(
            f"http://localhost:{port}/json/new?{quote(url, safe='')}",
            method="PUT",
        )
        raw = urllib.request.urlopen(request, timeout=5).read()
        tab_info = json.loads(raw)
        return tab_info.get("webSocketDebuggerUrl")
    except Exception as e:
        print(f"ERROR: Failed to create new tab: {e}", file=sys.stderr)
        return None


def close_tab(port, tab_id):
    """Close a tab via CDP HTTP API."""
    try:
        urllib.request.urlopen(
            f"http://localhost:{port}/json/close/{tab_id}", timeout=3
        ).read()
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser(
        description="Navigate to URL in Chrome and report any alert() dialog. Creates a new tab by default for multi-worker isolation."
    )
    parser.add_argument("url", help="Victim page URL to navigate to")
    parser.add_argument("--timeout", type=int, default=8, help="Seconds to wait for dialog (default: 8)")
    parser.add_argument("--cdp-port", type=int, default=9223, help="CDP port (default: 9223)")
    parser.add_argument("--tab-index", type=int, default=None, help="Reuse existing tab by index (NOT recommended with multiple workers)")
    args = parser.parse_args()

    use_new_tab = args.tab_index is None

    if use_new_tab:
        # Attach and enable Page events before navigating.  Navigating in
        # /json/new can fire alert() before the WebSocket listener exists.
        ws_url = create_new_tab(args.cdp_port, "about:blank")
        if not ws_url:
            sys.exit(2)
        print("[CDP] created isolated blank tab", file=sys.stderr)
        tab_id = None
        for tab in get_tabs(args.cdp_port):
            if tab.get("webSocketDebuggerUrl") == ws_url:
                tab_id = tab.get("id")
                break
    else:
        tabs = get_tabs(args.cdp_port)
        page_tabs = [t for t in tabs if t.get("type") == "page"]
        if not page_tabs:
            page_tabs = tabs
        if args.tab_index >= len(page_tabs):
            print(f"ERROR: tab index {args.tab_index} out of range, only {len(page_tabs)} page tabs", file=sys.stderr)
            sys.exit(2)
        ws_url = page_tabs[args.tab_index]["webSocketDebuggerUrl"]
        tab_id = page_tabs[args.tab_index].get("id")
        print(f"[CDP] reusing tab {args.tab_index}: {page_tabs[args.tab_index].get('url', '')[:60]}", file=sys.stderr)

    result = {"triggered": False, "message": None, "error": None}
    done = threading.Event()

    def on_message(ws, msg):
        try:
            data = json.loads(msg)
            if data.get("method") == "Page.javascriptDialogOpening":
                params = data.get("params", {})
                dialog_msg = params.get("message", "")
                dialog_type = params.get("type", "")
                print(f"[CDP] dialog fired: type={dialog_type} message={repr(dialog_msg)}", file=sys.stderr)
                ws.send(json.dumps({"id": 99, "method": "Page.handleJavaScriptDialog", "params": {"accept": False}}))
                result["message"] = dialog_msg
                result["triggered"] = True
                done.set()
        except Exception:
            pass

    def on_error(ws, error):
        result["error"] = str(error)
        done.set()

    def on_open(ws):
        ws.send(json.dumps({"id": 1, "method": "Page.enable"}))
        ws.send(json.dumps({"id": 2, "method": "Page.navigate", "params": {"url": args.url}}))
        print(f"[CDP] navigating to {args.url}", file=sys.stderr)

    ws = websocket.WebSocketApp(
        ws_url,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
    )

    t = threading.Thread(target=ws.run_forever, daemon=True)
    t.start()

    fired = done.wait(timeout=args.timeout)
    ws.close()

    if use_new_tab and tab_id:
        close_tab(args.cdp_port, tab_id)
        print(f"[CDP] closed tab {tab_id}", file=sys.stderr)

    if result["error"]:
        print(f"ERROR: {result['error']}", file=sys.stderr)
        sys.exit(2)

    if result["triggered"]:
        print(f"dialog_message={repr(result['message'])}")
        print(f"XSS dialog fired")
        sys.exit(0)
    else:
        print(f"No dialog within {args.timeout}s — XSS not triggered on {args.url}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
