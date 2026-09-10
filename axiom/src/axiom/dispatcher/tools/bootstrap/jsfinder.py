#!/usr/bin/env python3
"""jsfinder — 给一个 URL，抓取 JS 并提取九类信息，输出单个 JSON。

独立实现 mitan 2.x JSFinder：109 条分类正则（jsrules.json，随工具分发，
可 --rules 替换）+ 提取后校验器（is_api_route / 各云 AK / JWT / 身份证等），
过滤模板变量、静态资源与占位符噪声。仅依赖 Python3 标准库。

用法（bootstrap worker 调用约定：只给 URL，其余全默认）:
  python3 jsfinder.py https://target/
  python3 jsfinder.py https://target/ --out /path/to/jsfinder_result.json

输出: 单个 JSON，九类固定键（无命中为空数组）；进度打印在 stderr。
退出码: 0=有结果, 2=抓取成功但零命中, 3=入口全部不可达。
"""
from __future__ import annotations

import argparse
import base64
import http.client
import json
import os
import random
import re
import ssl
import sys
import threading
import time
import urllib.request
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.request import Request, HTTPSHandler

TOOL = "jsfinder"

UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 6.1; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/103.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:109.0) Gecko/20100101 Firefox/118.0",
]

CATEGORIES = ["API接口", "URL", "域名IP", "用户相关", "账号密码", "云Key", "Auth", "加解密", "其他"]

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

SCRIPT_RE = re.compile(r"""<script[^>]+src=["']([^"']+)["']""", re.I)
CSS_LINK_RE = re.compile(r"""<link[^>]+href=["\']([^"']+\.js[^"\']*)["\']""", re.I)
JS_IN_JS_RE = re.compile(
    r"""[`"'\(]((?:https?://[^\s"'`\)\]]+|(?:\.\./|\.?/)?[\w.\-@$/]+(?:/[\w.\-@$/]+)*)\.js(?:\?[^"'\`\)\]]*)?)[`"'\)\]]""",
    re.I,
)

PAGE_LIMIT = 20

# ---------------------------------------------------------------------------
# 校验器：移植 mitan jsrules.json 的 validator 字段（40 个）
# ---------------------------------------------------------------------------
_PLACEHOLDER_RE = re.compile(
    r"\$\{[^}]*\}|\{[a-zA-Z_][a-zA-Z0-9_]*\}|<[a-zA-Z_][a-zA-Z0-9_]*>|\b(?:xxx+|your[_-]?key|example|test[_-]?key|placeholder)\b",
    re.I,
)


def _is_api_route(value: str) -> bool:
    """与 mitan 导出口径一致：包容式，只剔构建产物与无信息段。"""
    path = value.split("?")[0]
    segments = [s for s in path.split("/") if s]
    if not segments:
        return False
    if path.lower().endswith((".js", ".css")):  # 构建产物（/assets/*.js 之类）
        return False
    if not any(re.search(r"[A-Za-z]{3,}", s) for s in segments):  # /a/b、/i/v1
        return False
    return True


def _is_ipv4(value: str) -> bool:
    parts = value.split(".")
    return len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 and str(int(p)) == p for p in parts)


def _is_http_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and "." in (parsed.netloc or "")


def _decode_b64(value: str) -> bytes | None:
    padded = value + "=" * (-len(value) % 4)
    try:
        return base64.b64decode(padded, validate=True)
    except Exception:
        try:
            return base64.urlsafe_b64decode(padded)
        except Exception:
            return None


def _printable_ratio(data: bytes) -> float:
    if not data:
        return 0.0
    printable = sum(1 for byte in data if 32 <= byte < 127 or byte in (9, 10, 13))
    return printable / len(data)


def _luhn(value: str) -> bool:
    digits = [int(c) for c in value]
    total, check = 0, digits.pop()
    for index, digit in enumerate(reversed(digits)):
        if index % 2 == 0:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return (total + check) % 10 == 0


_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CODES = "10X98765432"


def _is_id_card_18(value: str) -> bool:
    if not re.fullmatch(r"\d{17}[\dXx]", value):
        return False
    checksum = sum(int(c) * w for c, w in zip(value[:17], _ID_WEIGHTS))
    return _ID_CODES[checksum % 11] == value[17].upper()


def _is_jwt(value: str) -> bool:
    parts = value.split(".")
    if len(parts) != 3:
        return False
    header = _decode_b64(parts[0])
    if not header:
        return False
    try:
        obj = json.loads(header)
    except Exception:
        return False
    return isinstance(obj, dict) and "alg" in obj


def _generic_key(value: str, minimum: int = 16) -> bool:
    if len(value) < minimum or not re.fullmatch(r"[A-Za-z0-9+/=_\-]+", value):
        return False
    if _PLACEHOLDER_RE.search(value) or not re.search(r"[A-Za-z]", value) or not re.search(r"\d", value):
        return False
    return True


VALIDATORS: dict[str, object] = {
    "always_valid": lambda v: True,
    "is_api_route": _is_api_route,
    "is_valid_aliyun_ak": lambda v: bool(re.fullmatch(r"LTAI[0-9A-Za-z]{12,22}", v)),
    "is_valid_aws_ak": lambda v: bool(re.fullmatch(r"(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}", v)),
    "is_valid_azure_ak": lambda v: bool(re.fullmatch(r"[A-Za-z0-9+/]{52,88}={0,2}", v) and bool(_decode_b64(v))),
    "is_valid_baidu_ak": lambda v: bool(re.fullmatch(r"[0-9a-fA-F]{32}", v)),
    "is_valid_bank_card": lambda v: bool(re.fullmatch(r"\d{13,19}", v)) and _luhn(v),
    "is_valid_base64_key": lambda v: (
        len(v) >= 20
        and bool(re.fullmatch(r"[A-Za-z0-9+/=_\-]+", v))
        and (data := _decode_b64(v)) is not None
        and _printable_ratio(data) >= 0.7
    ),
    "is_valid_weak_password": lambda v: (
        bool(re.fullmatch(r"\S{6,20}", v))
        and bool(re.search(r"[A-Za-z]", v))
        and bool(re.search(r"\d", v))
        and not re.search(r"[;(){}<>=|&$~^`\[\]\"']", v)
    ),
    "is_valid_cloud_key": lambda v: _generic_key(v, 16),
    "is_valid_dingtalk_id": lambda v: bool(re.fullmatch(r"ding[0-9a-z]{20,44}", v)),
    "is_valid_dingtalk_secret": lambda v: bool(re.fullmatch(r"[0-9a-fA-F]{32}", v)),
    "is_valid_email_v2": lambda v: bool(re.fullmatch(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", v)),
    "is_valid_github_token": lambda v: bool(re.fullmatch(r"gh[pousr]_[A-Za-z0-9]{36,251}", v)),
    "is_valid_google_api_key": lambda v: bool(re.fullmatch(r"AIza[0-9A-Za-z_\-]{35}", v)),
    "is_valid_http_url": _is_http_url,
    "is_valid_huwei_ak": lambda v: _generic_key(v, 16),
    "is_valid_id_card_15": lambda v: bool(re.fullmatch(r"\d{15}", v)),
    "is_valid_id_card_18": _is_id_card_18,
    "is_valid_ipv4": _is_ipv4,
    "is_valid_jwt": _is_jwt,
    "is_valid_landline": lambda v: bool(re.fullmatch(r"0\d{2,3}-?\d{7,8}", v)),
    "is_valid_oracle_ak": lambda v: _generic_key(v, 16),
    "is_valid_phone": lambda v: bool(re.fullmatch(r"1[3-9]\d{9}", v)),
    "is_valid_private_key": lambda v: "PRIVATE KEY-----" in v,
    "is_valid_redis": lambda v: bool(re.fullmatch(r"rediss?://\S+", v)),
    "is_valid_secret": lambda v: (
        len(v) >= 10
        and bool(re.fullmatch(r"[A-Za-z0-9+/=_\-@#$%!.:]+", v))
        and bool(re.search(r"[A-Za-z]", v))
        and bool(re.search(r"\d", v))
        and not _PLACEHOLDER_RE.search(v)
    ),
    "is_valid_slack_token": lambda v: bool(re.fullmatch(r"xox[abprs]-[A-Za-z0-9\-]{10,}", v)),
    "is_valid_sms_code": lambda v: bool(re.fullmatch(r"\d{4,8}", v)),
    "is_valid_ssh_key": lambda v: bool(re.fullmatch(r"ssh-(?:rsa|dss|ed25519) AAAA[0-9A-Za-z+/=]+", v)),
    "is_valid_stripe_key": lambda v: bool(re.fullmatch(r"[psr]k_(?:live|test)_[A-Za-z0-9]{16,247}", v)),
    "is_valid_tencent_ak": lambda v: bool(re.fullmatch(r"AKID[0-9A-Za-z]{13,30}", v)),
    "is_valid_toll_free": lambda v: bool(re.fullmatch(r"(?:400|800)\d{7}", v)),
    "is_valid_uc_key": lambda v: _generic_key(v, 16),
    "is_valid_url_or_path": lambda v: _is_http_url(v) or (v.startswith("/") and bool(re.search(r"[A-Za-z]{2,}", v))),
    "is_valid_uuid": lambda v: bool(re.fullmatch(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}", v)),
    "is_valid_wechat_appid": lambda v: bool(re.fullmatch(r"wx[0-9a-f]{16}", v)),
    "is_valid_wechat_secret": lambda v: bool(re.fullmatch(r"[0-9a-fA-F]{32}", v)),
    "is_valid_yun_key": lambda v: _generic_key(v, 16),
}


# ---------------------------------------------------------------------------
# 抓取
# ---------------------------------------------------------------------------
def build_opener(proxy: str):
    handlers = [HTTPSHandler(context=CTX)]
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener(*handlers)


_TLS = threading.local()


def _get_conn(scheme: str, netloc: str, timeout: float) -> http.client.HTTPConnection:
    cache = getattr(_TLS, "conns", None)
    if cache is None:
        cache = _TLS.conns = {}
    conn = cache.get((scheme, netloc))
    if conn is None:
        if scheme == "https":
            conn = http.client.HTTPSConnection(netloc, timeout=timeout, context=CTX)
        else:
            conn = http.client.HTTPConnection(netloc, timeout=timeout)
        cache[(scheme, netloc)] = conn
    return conn


def _drop_conn(scheme: str, netloc: str) -> None:
    cache = getattr(_TLS, "conns", None)
    if not cache:
        return
    conn = cache.pop((scheme, netloc), None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


def _decode_body(body: bytes, content_type: str) -> str:
    charset = "utf-8"
    for part in content_type.split(";"):
        part = part.strip().lower()
        if part.startswith("charset="):
            charset = part.split("=", 1)[1].strip("'\" ") or charset
    try:
        return body.decode(charset, "replace")
    except LookupError:
        return body.decode("utf-8", "replace")


def _urllib_fetch(opener, url: str, cookie: str, timeout: float, retries: int) -> tuple[int, str, str]:
    """代理模式回退：无 keep-alive，仅在显式 --proxy 时使用。"""
    headers = {"User-Agent": random.choice(UA_POOL), "Accept": "*/*"}
    if cookie:
        headers["Cookie"] = cookie
    err = ""
    for attempt in range(retries + 1):
        try:
            req = Request(url, headers=headers)
            with opener.open(req, timeout=timeout) as resp:
                body = resp.read()
                content_type = resp.headers.get("Content-Type") or ""
                return resp.status, _decode_body(body, content_type), content_type
        except Exception as exc:  # noqa: BLE001 —— 网络异常逐类重试
            err = f"{type(exc).__name__}: {exc}"
            time.sleep(0.5 * (attempt + 1))
    return -1, "", err


def fetch(opener, url: str, cookie: str, timeout: float, retries: int, proxy: str = "") -> tuple[int, str, str]:
    if proxy:
        return _urllib_fetch(opener, url, cookie, timeout, retries)
    ua = random.choice(UA_POOL)
    err = ""
    for attempt in range(retries + 1):
        parsed = urlparse(url)
        for _hop in range(4):  # 手动跟随重定向
            try:
                conn = _get_conn(parsed.scheme, parsed.netloc, timeout)
                path = parsed.path or "/"
                if parsed.query:
                    path += "?" + parsed.query
                headers = {"User-Agent": ua, "Accept": "*/*", "Accept-Encoding": "identity"}
                if cookie:
                    headers["Cookie"] = cookie
                conn.request("GET", path, headers=headers)
                resp = conn.getresponse()
                body = resp.read()
                content_type = resp.headers.get("Content-Type") or ""
            except Exception as exc:  # noqa: BLE001 —— 连接级错误：丢弃复用连接后整轮重试
                err = f"{type(exc).__name__}: {exc}"
                _drop_conn(parsed.scheme, parsed.netloc)
                break
            if resp.status in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
                url = urljoin(url, resp.headers.get("Location"))
                parsed = urlparse(url)
                continue
            return resp.status, _decode_body(body, content_type), content_type
        else:
            return -1, "", "too many redirects"
        time.sleep(0.5 * (attempt + 1))
    return -1, "", err


def looks_like_html(url: str, content_type: str) -> bool:
    if "text/html" in content_type.lower():
        return True
    path = urlparse(url).path
    return path == "" or path.endswith((".html", ".htm", "/"))


def is_binary_type(content_type: str) -> bool:
    return bool(re.search(r"image/|font/|audio/|video/|octet-stream", content_type, re.I))


ROUTER_PATH_RE = re.compile(
    r"""path\s*:\s*["'`]([A-Za-z0-9_$\-][A-Za-z0-9_$\-/:*{}.\[\]]{0,150})["'`]"""
)
CALL_SITE_RE = re.compile(
    r"""\.\s*(?P<method>get|post|put|delete|patch)\s*\(\s*["'`](?P<url>[^"'`\s]{3,300})["'`]""",
    re.I,
)


def _object_keys(snippet: str) -> list[str]:
    match = re.search(r"\{([^{}]*)\}", snippet)
    if not match:
        return []
    keys: list[str] = []
    for part in match.group(1).split(","):
        part = part.strip()
        shorthand = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)", part)
        if shorthand:
            keys.append(shorthand.group(1))
            continue
        keyed = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s*:", part)
        if keyed:
            keys.append(keyed.group(1))
    return keys

def extract_call_sites(body: str) -> set[str]:
    """axios/fetch 风格调用点：METHOD path | query:.. | body:.. | header:.."""
    out: set[str] = set()
    for match in CALL_SITE_RE.finditer(body):
        url = match.group("url")
        if not url.startswith(("/", "${", "http")):
            continue
        method = match.group("method").upper()
        tail = body[match.end():match.end() + 500]
        parts = [f"{method} {url}"]
        query = sorted(set(re.findall(r"[?&]([A-Za-z_][A-Za-z0-9_]*)=", url)))
        if query:
            parts.append("query:" + ",".join(query))
        body_keys = _object_keys(tail)
        if body_keys:
            parts.append("body:" + ",".join(body_keys))
        if re.search(r"content-type", tail, re.I):
            parts.append("header:Content-Type")
        out.add(" | ".join(parts))
    return out


def extract_router_paths(body: str) -> set[str]:
    """vue/react-router 的 path: '...' 声明（相对路径、:param、* 通配）。"""
    out: set[str] = set()
    for match in ROUTER_PATH_RE.finditer(body):
        value = match.group(1)
        if "/" in value or ":" in value or "*" in value:
            out.add(value)
    return out


_SOURCEMAP_RE = re.compile(r"//[#@]\s*sourceMappingURL=(\S+)")


def sourcemap_candidates(url: str, body: str) -> list[str]:
    candidates: list[str] = []
    match = _SOURCEMAP_RE.search(body)
    if match:
        candidates.append(urljoin(url, match.group(1)))
    if urlparse(url).path.endswith(".js"):
        candidates.append(url + ".map")
    return candidates


def load_sourcemap(opener, map_url: str, cfg) -> list[str]:
    """拉取 .map 并返回 sourcesContent（原始 TS/JSX 源码，API 字符串最全）。"""
    status, body, _ct = fetch(opener, map_url, cfg.cookie, cfg.timeout, cfg.retries, cfg.proxy)
    if status != 200 or not body.lstrip().startswith("{"):
        return []
    try:
        data = json.loads(body)
    except ValueError:
        return []
    contents = data.get("sourcesContent")
    if not isinstance(contents, list):
        return []
    return [text for text in contents if isinstance(text, str) and len(text) > 20]


# ---------------------------------------------------------------------------
# 并发收集 + 提取（抓取与规则提取在 worker 内完成，第二层发现回推队列）
# ---------------------------------------------------------------------------
def load_rules(path: Path, only: set[str] | None):
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    rules = []
    for item in raw:
        if not item.get("enabled", True) or not item.get("categoryEnabled", True):
            continue
        if only and item["category"] not in only:
            continue
        try:
            rules.append((item, re.compile(item["pattern"])))
        except re.error:
            print(f"[!] 跳过坏规则：{item.get('subtype') or str(item.get('pattern'))[:40]}", file=sys.stderr)
    return rules


# 密钥类校验器：捕获值必须独立成词——紧邻字符是 [A-Za-z0-9+/=] 时，
# 说明它只是更长 base64/哈希串的切片（WASM/压缩 JS 常见），一律丢弃。
KEY_LIKE = frozenset({
    "is_valid_aliyun_ak", "is_valid_aws_ak", "is_valid_azure_ak", "is_valid_baidu_ak",
    "is_valid_bank_card", "is_valid_base64_key", "is_valid_ci_key", "is_valid_cloud_key",
    "is_valid_dingtalk_id", "is_valid_dingtalk_secret", "is_valid_github_token",
    "is_valid_google_api_key", "is_valid_huwei_ak", "is_valid_jwt", "is_valid_oracle_ak",
    "is_valid_private_key", "is_valid_redis", "is_valid_secret", "is_valid_slack_token",
    "is_valid_ssh_key", "is_valid_stripe_key", "is_valid_tencent_ak", "is_valid_uc_key",
    "is_valid_wechat_appid", "is_valid_wechat_secret", "is_valid_yun_key", "is_valid_uuid",
    "is_valid_phone", "is_valid_landline", "is_valid_toll_free", "is_valid_email_v2",
    "is_valid_id_card_15", "is_valid_id_card_18", "is_valid_sms_code", "is_valid_weak_password",
})

_BOUNDARY_RE = re.compile(r"[A-Za-z0-9+/=]")
_DEBUG = os.environ.get("JSFINDER_DEBUG") == "1"
_DEBUG_REJECTS: list[tuple[str, str]] = []


def run_rules(text: str, rules):
    hits: dict[str, set] = {}
    for item, pattern in rules:
        capture = item.get("captureGroup", 0)
        validator_name = item.get("validator") or "always_valid"
        validator = VALIDATORS.get(validator_name, lambda v: True)
        key_like = validator_name in KEY_LIKE
        minimum = item.get("minLength") or 2
        bucket = hits.setdefault(item["category"], set())
        for match in pattern.finditer(text):
            value = match.group(capture) if capture else match.group(0)
            value = (value or "").strip().strip("\"'")
            if key_like:
                start, end = match.span(capture) if capture else match.span()
                if (start > 0 and _BOUNDARY_RE.match(text[start - 1])) or (
                    end < len(text) and _BOUNDARY_RE.match(text[end])
                ):
                    if _DEBUG and len(_DEBUG_REJECTS) < 300:
                        _DEBUG_REJECTS.append((item["category"], value[:80]))
                    continue
            if len(value) < minimum:
                continue
            if not validator(value):
                continue
            bucket.add(value)
    return hits


def mine(opener, entries: list[str], cfg, rules):
    total: dict[str, set] = {c: set() for c in CATEGORIES}
    stats = {"js_files": 0, "pages": 0, "errors": 0, "maps": 0}
    deadline = time.monotonic() + cfg.max_seconds
    done = 0
    lock = threading.Lock()
    cv = threading.Condition(lock)
    pending: deque[str] = deque(entries)
    seen: set[str] = set(entries)
    active = 0
    entry_set = {e.rstrip("/") for e in entries}
    maps_seen: set[str] = set()

    def submit(url: str) -> None:
        with cv:
            if url in seen:
                return
            seen.add(url)
            path = urlparse(url).path
            if path.endswith(".js") or ".js?" in url:
                if stats["js_files"] >= cfg.max_js:
                    return
                stats["js_files"] += 1
            elif stats["pages"] >= PAGE_LIMIT:
                return
            else:
                stats["pages"] += 1
            pending.append(url)
            cv.notify_all()

    def worker() -> None:
        nonlocal active, done
        while True:
            with cv:
                while not pending and active > 0:
                    cv.wait(timeout=1)
                if not pending and active == 0:
                    return
                if time.monotonic() >= deadline:
                    return
                url = pending.popleft()
                active += 1
            try:
                if cfg.delay:
                    time.sleep(cfg.delay)
                status, body, content_type = fetch(opener, url, cfg.cookie, cfg.timeout, cfg.retries, cfg.proxy)
                if status != 200 or not body:
                    if status == -1:
                        with lock:
                            stats["errors"] += 1
                    continue
                if is_binary_type(content_type):
                    continue
                hits = run_rules(body, rules)
                if hits:
                    with lock:
                        for category, values in hits.items():
                            total.setdefault(category, {}).update(values)
                extra = extract_call_sites(body) | extract_router_paths(body)
                if extra:
                    with lock:
                        total.setdefault("API接口", {}).update(extra)
                for map_url in sourcemap_candidates(url, body):
                    with lock:
                        if map_url in maps_seen:
                            continue
                        maps_seen.add(map_url)
                    for text in load_sourcemap(opener, map_url, cfg):
                        sm_hits = run_rules(text, rules)
                        sm_extra = extract_call_sites(text) | extract_router_paths(text)
                        with lock:
                            for category, values in sm_hits.items():
                                total.setdefault(category, {}).update(values)
                            if sm_extra:
                                total.setdefault("API接口", {}).update(sm_extra)
                            stats["maps"] += 1
                if looks_like_html(url, content_type) or url.rstrip("/") in entry_set:
                    for match in SCRIPT_RE.findall(body) + CSS_LINK_RE.findall(body):
                        absolute = urljoin(url, match)
                        if absolute.startswith("http"):
                            submit(absolute)
                for match in JS_IN_JS_RE.findall(body):
                    absolute = urljoin(url, match)
                    if absolute.startswith("http"):
                        submit(absolute)
            finally:
                with cv:
                    active -= 1
                    done += 1
                    if done % 50 == 0:
                        print(
                            f"[+] 进度：完成 {done}（js {stats['js_files']}/{cfg.max_js}）",
                            file=sys.stderr,
                        )
                    cv.notify_all()

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, cfg.threads))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return total, stats


_HTTP_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}


def _api_path(entry: str) -> str:
    first = entry.split(" | ", 1)[0]
    verb, _, remainder = first.partition(" ")
    return remainder if verb in _HTTP_METHODS else first


def _group_api(values: list[str], targets: list[str]) -> dict:
    """API接口按域分组：域 -> 路径前缀(前两段) -> 排序条目，未分组单列。"""
    # 多目标共享同一入口域时，相对路径 API 归属该域；不同域才落入未归属。
    target_netlocs = {urlparse(t).netloc for t in targets if urlparse(t).netloc}
    origin = target_netlocs.pop() if len(target_netlocs) == 1 else (
        urlparse(targets[0]).netloc if len(targets) == 1 else "_未归属"
    )
    grouped: dict[str, dict[str, list[str]]] = {}
    for entry in values:
        path = _api_path(entry)
        if path.startswith(("http://", "https://")):
            parsed = urlparse(path)
            host, rest = parsed.netloc, path.split(parsed.netloc, 1)[1]
        else:
            host, rest = origin, path
        clean = rest.split("?")[0].lstrip("/")
        segments = [s for s in clean.split("/") if s and not s.startswith(("$", "{", ":"))]
        prefix = "/".join(segments[:2]) if segments and segments[0] == "api" else (segments[0] if segments else "")
        grouped.setdefault(host, {}).setdefault(prefix or "_未分组", []).append(entry)
    return {
        host: {prefix: sorted(entries) for prefix, entries in sorted(prefixes.items())}
        for host, prefixes in sorted(grouped.items())
    }


def _count_entries(value) -> int:
    if isinstance(value, dict):
        return sum(_count_entries(v) for v in value.values())
    return len(value)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main() -> int:
    script_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        prog=TOOL,
        description="给一个 URL，抓取 JS 并提取九类信息，输出单个 JSON（mitan JSFinder 独立实现）。",
    )
    parser.add_argument("urls", nargs="+", help="目标 URL（一个或多个）")
    parser.add_argument("--cookie", default="", help="登录态 Cookie 串")
    parser.add_argument("--threads", type=int, default=30)
    parser.add_argument("--delay", type=float, default=0.0, help="每个请求后休眠秒数")
    parser.add_argument("--max-js", type=int, default=1500, dest="max_js")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--max-seconds", type=float, default=180.0, dest="max_seconds", help="总时限（秒），到点立即产出已有结果")
    parser.add_argument("--rules", default=str(script_dir / "jsrules.json"))
    parser.add_argument("--only", default="", help="只提取这些类别（逗号分隔），默认九类全跑")
    parser.add_argument("--proxy", default="")
    args = parser.parse_args()

    only = {c.strip() for c in args.only.split(",") if c.strip()} or None
    try:
        rules = load_rules(Path(args.rules), only)
    except FileNotFoundError:
        print(f"[!] 规则文件不存在：{args.rules}", file=sys.stderr)
        return 3
    print(f"[*] 规则：{len(rules)} 条（类别 {sorted({item['category'] for item, _ in rules})}）", file=sys.stderr)

    opener = build_opener(args.proxy)
    started = time.monotonic()
    total, stats = mine(opener, args.urls, args, rules)
    elapsed = round(time.monotonic() - started, 1)

    categories = {c: sorted(total.get(c, set())) for c in CATEGORIES}
    for value in (*categories["API接口"], *categories["URL"]):
        if value.startswith(("http://", "https://")):
            netloc = urlparse(value).netloc
            if netloc and "." in netloc:
                categories["域名IP"].append(netloc)
    categories["域名IP"] = sorted(set(categories["域名IP"]))
    categories["API接口"] = _group_api(categories["API接口"], args.urls)
    hits_total = sum(_count_entries(v) for v in categories.values())
    result = {
        "tool": TOOL,
        "targets": list(args.urls),
        "scanned_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "elapsed_seconds": elapsed,
        "stats": {"js_files": stats["js_files"], "pages": stats["pages"], "fetch_errors": stats["errors"], "hits": hits_total},
        "categories": categories,
    }
    run_dir = Path(os.environ.get("AXIOM_RUN_DIR") or ".")
    run_dir.mkdir(parents=True, exist_ok=True)
    out_path = run_dir / "jsfinder_result.json"
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[*] JS {stats['js_files']} 个 / 页面 {stats['pages']} 个 / sourcemap {stats['maps']} 个 / 请求失败 {stats['errors']} / 耗时 {elapsed}s", file=sys.stderr)
    for category in CATEGORIES:
        if categories[category]:
            print(f"    [{category}] {_count_entries(categories[category])} 条", file=sys.stderr)
    print(f"[*] 共 {hits_total} 条 -> {out_path}", file=sys.stderr)
    if _DEBUG and _DEBUG_REJECTS:
        print("[debug] 边界拦截样本（前 40）：", file=sys.stderr)
        for category, value in _DEBUG_REJECTS[:40]:
            print(f"    [{category}] {value}", file=sys.stderr)
    if stats["js_files"] == 0 and stats["errors"] >= max(1, len(args.urls)):
        print("[✗] jsfinder 失败：入口不可达，未产出有效结果", file=sys.stderr)
        return 3
    print(f"[✓] jsfinder 完成：九类共 {hits_total} 条，结果文件 {out_path.resolve()}")
    return 0 if hits_total else 2


if __name__ == "__main__":
    sys.exit(main())
