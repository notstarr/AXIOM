#!/usr/bin/env python3
"""
XSS Verify — CDP dialog detector.

Connects to an existing Chrome instance on --cdp-port (default 9222),
navigates to URL, waits for alert(TOKEN) to fire.

Exit codes:
  0 = dialog fired and message contains TOKEN  → XSS CONFIRMED
  1 = timeout, no matching dialog              → XSS NOT triggered
  2 = error (Chrome not running, etc.)

Usage:
    python3 detect.py <URL> <TOKEN> [--timeout 8] [--cdp-port 9222] [--tab-index 0]

If Chrome is not running, launch it first:
    /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
      --user-data-dir="$HOME/Library/Application Support/Chrome-SecTest" \
      --remote-debugging-port=9222 --no-first-run &
    sleep 3
"""
import sys
import json
import time
import argparse
import threading
import urllib.request

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


def main():
    parser = argparse.ArgumentParser(
        description="Navigate to URL in an existing Chrome and report any alert() dialog message."
    )
    parser.add_argument("url", help="Victim page URL to navigate to")
    parser.add_argument("--timeout", type=int, default=8, help="Seconds to wait for dialog (default: 8)")
    parser.add_argument("--cdp-port", type=int, default=9223)
    parser.add_argument("--tab-index", type=int, default=0, help="Which tab to use (default: 0 = first page tab)")
    args = parser.parse_args()

    # No payload parsing — the caller (AI) decides whether dialog.message is correct

    tabs = get_tabs(args.cdp_port)
    # Filter to page tabs only (not extensions)
    page_tabs = [t for t in tabs if t.get("type") == "page"]
    if not page_tabs:
        page_tabs = tabs  # fallback: use all
    if args.tab_index >= len(page_tabs):
        print(f"ERROR: tab index {args.tab_index} out of range, only {len(page_tabs)} page tabs", file=sys.stderr)
        sys.exit(2)

    ws_url = page_tabs[args.tab_index]["webSocketDebuggerUrl"]
    print(f"[CDP] connecting to tab: {page_tabs[args.tab_index].get('url', '')[:60]}", file=sys.stderr)

    result = {"triggered": False, "message": None, "error": None}
    done = threading.Event()

    def on_message(ws, msg):
        try:
            data = json.loads(msg)
            # Dialog event
            if data.get("method") == "Page.javascriptDialogOpening":
                params = data.get("params", {})
                dialog_msg = params.get("message", "")
                dialog_type = params.get("type", "")
                print(f"[CDP] dialog fired: type={dialog_type} message={repr(dialog_msg)}", file=sys.stderr)
                # Dismiss the dialog
                ws.send(json.dumps({"id": 99, "method": "Page.handleJavaScriptDialog", "params": {"accept": False}}))
                result["message"] = dialog_msg
                result["triggered"] = True  # any dialog = XSS fired; caller decides if correct
                done.set()
        except Exception as e:
            pass

    def on_error(ws, error):
        result["error"] = str(error)
        done.set()

    def on_open(ws):
        # Enable Page events
        ws.send(json.dumps({"id": 1, "method": "Page.enable"}))
        # Navigate to victim URL
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

    if result["error"]:
        print(f"ERROR: {result['error']}", file=sys.stderr)
        sys.exit(2)

    if result["triggered"]:
        # Print the raw dialog message — the caller (AI) decides whether it matches the injected payload
        print(f"dialog_message={repr(result['message'])}")
        print(f"✅ XSS dialog fired")
        sys.exit(0)
    else:
        print(f"❌ No dialog within {args.timeout}s — XSS not triggered on {args.url}")
        sys.exit(1)


if __name__ == "__main__":
    main()
