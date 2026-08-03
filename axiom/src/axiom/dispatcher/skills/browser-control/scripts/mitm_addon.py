"""
mitm_addon.py — mitmproxy addon，记录 HTTP 流量到 JSONL 文件

启动方式（由 explore skill 负责启动，AI 不需要手动运行）：
    mitmdump -s /path/to/mitm_addon.py --listen-port 18080 -q

环境变量：
    PROXY_FLOW_FILE   输出文件路径（默认 /tmp/proxy_flows.jsonl）
    MITM_TARGET_HOST  只记录包含此字符串的域名（如 target.example.com）
                      为空时记录所有非静态资源请求

写入格式（每行一个 JSON）：
    {
      "id":               "flow_xxxxxxxx",
      "ts":               1718000000.123,
      "method":           "GET",
      "url":              "https://...",
      "request_headers":  {...},
      "request_body":     "...",
      "status_code":      200,
      "response_headers": {...},
      "response_body":    "...(max 10000 chars)",
      "content_type":     "application/json"
    }
"""

import gzip
import fcntl
import json
import os
import time
import uuid
import zlib
from pathlib import Path

from mitmproxy import tls

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


FLOW_FILE = os.getenv("PROXY_FLOW_FILE", "/tmp/proxy_flows.jsonl")
TARGET_HOST = os.getenv("MITM_TARGET_HOST", "")
FLOW_MAX_BYTES = max(_env_int("PROXY_FLOW_MAX_BYTES", 64 * 1024 * 1024), 1024 * 1024)
FLOW_BACKUPS = min(max(_env_int("PROXY_FLOW_BACKUPS", 3), 0), 20)

# 直接跳过的静态资源后缀
SKIP_EXTENSIONS = {
    ".css", ".js", ".mjs", ".png", ".jpg", ".jpeg", ".gif",
    ".svg", ".ico", ".woff", ".woff2", ".ttf", ".eot", ".map",
    ".webp", ".avif", ".mp4", ".webm",
}

# 跳过的 Content-Type 前缀
SKIP_CONTENT_TYPES = (
    "image/", "font/", "text/css", "application/javascript",
    "text/javascript",
)

# CDN / 第三方域名关键字，TLS 透传（不解密）
PASSTHROUGH_KEYWORDS = (
    "cloudfront.net", "cloudflare.com", "akamaized.net", "fastly.net",
    "google.com", "googleapis.com", "gstatic.com", "googletagmanager.com",
    "sentry.io", "newrelic.com", "segment.com", "hotjar.com",
    "fonts.gstatic.com", "fonts.googleapis.com",
    "pendo.io", "mixpanel.com", "intercom.io", "hubspot.com",
)


def _should_passthrough(hostname: str) -> bool:
    if TARGET_HOST:
        # 白名单模式：只对目标域名解密，其余透传
        return TARGET_HOST.lower() not in hostname.lower()
    # 黑名单模式：已知 CDN/第三方透传
    h = hostname.lower()
    return any(kw in h for kw in PASSTHROUGH_KEYWORDS)


def _decode_body(content: bytes, content_encoding: str) -> bytes:
    try:
        if "gzip" in content_encoding:
            return gzip.decompress(content)
        if "deflate" in content_encoding:
            return zlib.decompress(content)
    except Exception:
        pass
    return content


def _body_text(content: bytes, content_type: str) -> str:
    charset = None
    if "charset=" in content_type:
        charset = content_type.split("charset=")[-1].split(";")[0].strip().strip('"\'')
    try:
        return content.decode(charset or "utf-8", errors="replace")[:10000]
    except Exception:
        return ""


def _rotate_flow_file(path: Path) -> None:
    if not path.exists():
        return
    if FLOW_BACKUPS <= 0:
        path.unlink(missing_ok=True)
        return
    Path(f"{path}.{FLOW_BACKUPS}").unlink(missing_ok=True)
    for index in range(FLOW_BACKUPS - 1, 0, -1):
        source = Path(f"{path}.{index}")
        if source.exists():
            os.replace(source, Path(f"{path}.{index + 1}"))
    os.replace(path, Path(f"{path}.1"))


def _append_record(record: dict) -> None:
    path = Path(FLOW_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    lock_path = Path(f"{path}.lock")
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            current_size = path.stat().st_size if path.exists() else 0
            if current_size and current_size + len(encoded) > FLOW_MAX_BYTES:
                _rotate_flow_file(path)
            with path.open("ab") as output:
                output.write(encoded)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


class TlsPassthrough:
    def tls_clienthello(self, data: tls.ClientHelloData):
        hostname = data.client_hello.sni
        if not hostname:
            addr = data.context.server.address
            if addr:
                hostname = addr[0] if isinstance(addr, tuple) else str(addr)
        if hostname and _should_passthrough(hostname):
            data.ignore_connection = True


class FlowRecorder:
    def response(self, flow):
        url = flow.request.pretty_url
        path = flow.request.path.split("?")[0].lower()

        # 跳过静态资源
        if any(path.endswith(ext) for ext in SKIP_EXTENSIONS):
            return

        ct = flow.response.headers.get("content-type", "") if flow.response else ""
        if any(ct.startswith(skip) for skip in SKIP_CONTENT_TYPES):
            return

        # 目标域名过滤
        if TARGET_HOST and TARGET_HOST.lower() not in flow.request.pretty_host.lower():
            return

        # 解码响应体
        raw = flow.response.content if flow.response else b""
        ce = (flow.response.headers.get("content-encoding", "") if flow.response else "")
        raw = _decode_body(raw, ce)
        resp_body = _body_text(raw, ct)

        # 请求体
        try:
            req_body = flow.request.get_text() or ""
        except Exception:
            req_body = ""

        record = {
            "id": f"flow_{uuid.uuid4().hex[:8]}",
            "ts": time.time(),
            "method": flow.request.method,
            "url": url,
            "request_headers": dict(flow.request.headers),
            "request_body": req_body[:5000],
            "status_code": flow.response.status_code if flow.response else 0,
            "response_headers": dict(flow.response.headers) if flow.response else {},
            "response_body": resp_body,
            "content_type": ct,
        }

        try:
            _append_record(record)
        except Exception:
            pass


addons = [TlsPassthrough(), FlowRecorder()]
