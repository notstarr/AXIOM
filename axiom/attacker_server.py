#!/usr/bin/env python3
"""攻击者捕获服务器：记录所有请求（含完整查询串、Headers、Body）到 JSONL"""
import http.server
import json
import sys
from datetime import datetime
from urllib.parse import unquote

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 18991
LOG = "/Users/hexin/vscode/xrayAI/axiom/attacker_captures.jsonl"

class H(http.server.BaseHTTPRequestHandler):
    def _log(self):
        rec = {
            "ts": datetime.now().isoformat(),
            "method": self.command,
            "path": self.path,
            "path_unquoted": unquote(self.path),
            "headers": dict(self.headers),
            "client": self.client_address[0],
        }
        cl = self.headers.get("Content-Length")
        if cl:
            try:
                rec["body"] = self.rfile.read(int(cl)).decode("utf-8", "replace")
            except Exception as e:
                rec["body_err"] = str(e)
        with open(LOG, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        # 返回一个简单 HTML 页面
        html = b"<!doctype html><html><head><meta charset=utf-8><title>Preview</title></head><body><h1>Preview OK</h1></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(html)

    do_GET = _log
    do_POST = _log
    do_OPTIONS = _log

    def log_message(self, *a):
        pass

if __name__ == "__main__":
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), H)
    print(f"attacker server on {PORT}")
    srv.serve_forever()
