from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import platform
import secrets
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse
from urllib.request import urlopen

from playwright.async_api import (
    Browser,
    BrowserContext,
    ElementHandle,
    Frame,
    Page,
    Playwright,
    async_playwright,
)

from axiom.modules.tasks.origin import normalize_scope_target, scope_entry_matches_host


BROWSER_TOOL_NAMES = (
    "browser_open_page",
    "browser_list_pages",
    "browser_close_page",
    "browser_navigate",
    "browser_read_page",
    "browser_snapshot",
    "browser_click",
    "browser_fill",
    "browser_press",
    "browser_hover",
    "browser_scroll",
    "browser_select",
    "browser_check",
    "browser_upload_file",
    "browser_wait",
    "browser_screenshot",
    "browser_read_network",
    "browser_read_console",
    "browser_evaluate",
    "browser_get_credentials",
)

INTERACTIVE_SELECTOR = (
    "a,button,input:not([type=hidden]),select,textarea,summary,"
    "[role=button],[role=link],[role=textbox],[role=checkbox],[role=radio],"
    "[role=combobox],[role=menuitem],[role=option],[role=tab],[role=switch],"
    "[contenteditable=true],[onclick],[tabindex]:not([tabindex='-1'])"
)
MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_CHARS = 128_000
MAX_SNAPSHOT_REFS = 600
DEFAULT_ACTION_TIMEOUT_MS = 10_000
MAX_ACTION_TIMEOUT_MS = 60_000
SENSITIVE_HEADERS = {
    "authorization",
    "cookie",
    "proxy-authorization",
    "set-cookie",
    "x-api-key",
}


class BrowserBrokerError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(slots=True)
class BrowserLeaseHandle:
    socket_path: str
    token: str
    lease_id: str


@dataclass(slots=True)
class RefState:
    ref: str
    generation: int
    handle: ElementHandle
    role: str
    accessible_name: str
    text: str
    attributes: dict[str, str]
    allowed_actions: tuple[str, ...]
    frame_id: str
    dom_key: str


@dataclass(slots=True)
class PageState:
    page_id: str
    page: Page
    target_id: str
    generation: int = 1
    snapshot_sequence: int = 0
    ref_sequence: int = 0
    frame_sequence: int = 0
    frame_ids: dict[int, str] = field(default_factory=dict)
    refs: dict[str, RefState] = field(default_factory=dict)
    refs_by_dom_key: dict[str, str] = field(default_factory=dict)
    snapshot_lines: dict[str, list[str]] = field(default_factory=dict)
    network_entries: list[dict[str, Any]] = field(default_factory=list)
    console_entries: list[dict[str, Any]] = field(default_factory=list)
    download_entries: list[dict[str, Any]] = field(default_factory=list)
    network_cursor: int = 0
    console_cursor: int = 0
    download_cursor: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass(slots=True)
class LeaseState:
    token: str
    lease_id: str
    task_id: str
    intent_id: str
    run_dir: Path
    scope: dict[str, Any]
    cdp_url: str
    pages: dict[str, PageState] = field(default_factory=dict)
    page_sequence: int = 0


def browser_skill_directory(paths: list[str]) -> Path | None:
    return next(
        (
            Path(value)
            for value in paths
            if Path(value).name == "browser-control"
            and (Path(value) / "SKILL.md").is_file()
        ),
        None,
    )


def target_allowed(scope: dict[str, Any], url_text: str) -> bool:
    if not scope or scope.get("enabled") is False:
        return True
    parsed = urlparse(url_text)
    if parsed.scheme not in {"http", "https", "about"}:
        return False
    if parsed.scheme == "about":
        return parsed.path == "blank"
    hostname = (parsed.hostname or "").strip().lower().rstrip(".")
    if not hostname:
        return False

    def host(value: object) -> str:
        raw = str(value or "").strip().lower()
        if not raw:
            return ""
        try:
            return normalize_scope_target(raw)
        except Exception:
            # Compatibility for old in-memory callers that still pass a URL
            # instead of the normalized host stored by Task Scope.
            parsed_value = urlparse(raw if "://" in raw else f"//{raw}")
            return (parsed_value.hostname or "").lower().rstrip(".")

    excluded = [host(item) for item in scope.get("exclude", []) if host(item)]
    if any(scope_entry_matches_host(item, hostname) for item in excluded):
        return False
    targets = [host(item) for item in scope.get("targets", []) if host(item)]
    if not targets:
        return True
    return any(scope_entry_matches_host(item, hostname) for item in targets)


def redact_headers(headers: dict[str, Any], *, include_sensitive: bool) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, value in headers.items():
        normalized = str(name).lower()
        result[str(name)] = (
            str(value)
            if include_sensitive or normalized not in SENSITIVE_HEADERS
            else "[已脱敏]"
        )
    return result


class BrowserBrokerManager:
    """Runner-owned bridge between Pi native tools and one Playwright session."""

    def __init__(self, *, socket_dir: Path | None = None):
        directory = socket_dir or Path(tempfile.gettempdir())
        name = f"axiom-browser-{os.getpid()}-{uuid.uuid4().hex[:8]}.sock"
        candidate = directory / name
        # macOS limits AF_UNIX paths to roughly 104 bytes; pytest and some
        # deployment roots are much longer than that.
        if len(os.fsencode(candidate)) >= 100:
            candidate = Path(tempfile.gettempdir()) / name
        self.socket_path = candidate
        self._secret = secrets.token_urlsafe(32)
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._closed = False
        self._start_error: BaseException | None = None

    def register_lease(
        self,
        *,
        lease_id: str,
        task_id: str,
        intent_id: str,
        run_dir: str,
        scope: dict[str, Any],
        cdp_url: str,
    ) -> BrowserLeaseHandle:
        self._ensure_started()
        token = secrets.token_urlsafe(32)
        response = self._request(
            {
                "op": "_register_lease",
                "manager_secret": self._secret,
                "token": token,
                "lease_id": lease_id,
                "task_id": task_id,
                "intent_id": intent_id,
                "run_dir": run_dir,
                "scope": scope,
                "cdp_url": cdp_url,
            }
        )
        if not response.get("ok"):
            error = response.get("error") or {}
            raise BrowserBrokerError(
                str(error.get("code") or "broker_registration_failed"),
                str(error.get("message") or "Browser Broker registration failed"),
                retryable=bool(error.get("retryable")),
            )
        return BrowserLeaseHandle(str(self.socket_path), token, lease_id)

    def close_lease(self, handle: BrowserLeaseHandle) -> None:
        if self._closed or not self.socket_path.exists():
            return
        with contextlib.suppress(OSError, BrowserBrokerError):
            self._request(
                {
                    "op": "_close_lease",
                    "manager_secret": self._secret,
                    "token": handle.token,
                },
                timeout=8.0,
            )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.socket_path.exists():
            with contextlib.suppress(OSError, BrowserBrokerError):
                self._request(
                    {"op": "_shutdown", "manager_secret": self._secret},
                    timeout=5.0,
                )
        if self._thread is not None:
            self._thread.join(timeout=8.0)
        self.socket_path.unlink(missing_ok=True)

    def _ensure_started(self) -> None:
        if self._closed:
            raise BrowserBrokerError("broker_closed", "Browser Broker is closed")
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._thread_main,
                name="axiom-browser-broker",
                daemon=True,
            )
            self._thread.start()
        if not self._ready.wait(timeout=10.0):
            raise BrowserBrokerError(
                "broker_start_timeout",
                "Browser Broker did not become ready",
                retryable=True,
            )
        if self._start_error is not None:
            raise BrowserBrokerError(
                "broker_start_failed",
                str(self._start_error),
                retryable=True,
            )

    def _thread_main(self) -> None:
        try:
            asyncio.run(
                BrowserBrokerServer(
                    manager_secret=self._secret,
                    socket_path=self.socket_path,
                    ready=self._ready,
                ).serve()
            )
        except BaseException as exc:
            self._start_error = exc
            self._ready.set()

    def _request(self, payload: dict[str, Any], *, timeout: float = 15.0) -> dict[str, Any]:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(self.socket_path))
            client.sendall(encoded)
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = client.recv(64 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_REQUEST_BYTES:
                    raise BrowserBrokerError("broker_response_too_large", "Browser Broker response is too large")
                if b"\n" in chunk:
                    break
        raw = b"".join(chunks).split(b"\n", 1)[0]
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BrowserBrokerError("broker_invalid_response", str(exc), retryable=True) from exc
        if not isinstance(value, dict):
            raise BrowserBrokerError("broker_invalid_response", "Browser Broker returned a non-object")
        return value


class BrowserBrokerServer:
    def __init__(
        self,
        *,
        manager_secret: str,
        socket_path: Path,
        ready: threading.Event,
    ):
        self.manager_secret = manager_secret
        self.socket_path = socket_path
        self.ready = ready
        self.leases: dict[str, LeaseState] = {}
        self._shutdown = asyncio.Event()
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._cdp_url = ""
        self._chrome_pid: int | None = None
        self._chrome_port: int | None = None
        self._chrome_profile: Path | None = None
        self._chrome_pid_file: Path | None = None
        self._connect_lock = asyncio.Lock()

    async def serve(self) -> None:
        self.socket_path.unlink(missing_ok=True)
        server = await asyncio.start_unix_server(
            self._handle_client,
            path=str(self.socket_path),
            limit=MAX_REQUEST_BYTES + 1,
        )
        self.socket_path.chmod(0o600)
        self.ready.set()
        try:
            async with server:
                await self._shutdown.wait()
        finally:
            server.close()
            await server.wait_closed()
            for token in list(self.leases):
                await self._close_lease(token)
            if self._playwright is not None:
                with contextlib.suppress(Exception):
                    await self._playwright.stop()
            self._stop_owned_chrome()
            self.socket_path.unlink(missing_ok=True)

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if not raw or len(raw) > MAX_REQUEST_BYTES:
                raise BrowserBrokerError("invalid_request", "Browser Broker request is empty or too large")
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise BrowserBrokerError("invalid_request", "Browser Broker request must be an object")
            operation = asyncio.create_task(self._dispatch(payload))
            disconnected = asyncio.create_task(reader.read())
            done, _pending = await asyncio.wait(
                {operation, disconnected},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if disconnected in done and operation not in done:
                operation.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await operation
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
                return
            disconnected.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await disconnected
            response = await operation
        except BrowserBrokerError as exc:
            response = self._error(exc.code, str(exc), retryable=exc.retryable)
        except (asyncio.TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            response = self._error("invalid_request", str(exc))
        except BaseException as exc:
            response = self._error("broker_internal_error", str(exc), retryable=True)
        encoded = json.dumps(response, ensure_ascii=False, default=str).encode("utf-8") + b"\n"
        if len(encoded) > MAX_REQUEST_BYTES:
            encoded = json.dumps(
                self._error(
                    "broker_response_too_large",
                    "浏览器结果过大，请缩小Snapshot、Network过滤条件或读取上限",
                ),
                ensure_ascii=False,
            ).encode("utf-8") + b"\n"
        writer.write(encoded)
        with contextlib.suppress(Exception):
            await writer.drain()
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()

    async def _dispatch(self, payload: dict[str, Any]) -> dict[str, Any]:
        operation = str(payload.get("op") or "")
        if operation.startswith("_"):
            self._require_manager(payload)
            if operation == "_register_lease":
                return await self._register_lease(payload)
            if operation == "_close_lease":
                await self._close_lease(str(payload.get("token") or ""))
                return self._ok("Browser Lease 已关闭")
            if operation == "_shutdown":
                self._shutdown.set()
                return self._ok("Browser Broker 正在停止")
            raise BrowserBrokerError("unknown_operation", f"Unknown control operation: {operation}")

        token = str(payload.get("token") or "")
        lease = self.leases.get(token)
        if lease is None:
            raise BrowserBrokerError("lease_not_found", "Browser Lease不存在或已结束")
        params = payload.get("params")
        if not isinstance(params, dict):
            params = {}
        timeout_ms = self._timeout_ms(params)
        try:
            return await asyncio.wait_for(
                self._tool(lease, operation, params),
                timeout=(timeout_ms / 1000) + 2,
            )
        except asyncio.TimeoutError as exc:
            raise BrowserBrokerError(
                "operation_timeout",
                f"浏览器操作超过 {timeout_ms}ms",
                retryable=True,
            ) from exc

    def _require_manager(self, payload: dict[str, Any]) -> None:
        if not secrets.compare_digest(
            str(payload.get("manager_secret") or ""), self.manager_secret
        ):
            raise BrowserBrokerError("forbidden", "Invalid Browser Broker manager credential")

    async def _register_lease(self, payload: dict[str, Any]) -> dict[str, Any]:
        token = str(payload.get("token") or "")
        lease_id = str(payload.get("lease_id") or "")
        if not token or not lease_id:
            raise BrowserBrokerError("invalid_registration", "Browser Lease token and ID are required")
        old = self.leases.get(token)
        if old is not None:
            return self._ok("Browser Lease 已存在")
        run_dir = Path(str(payload.get("run_dir") or "")).resolve()
        run_dir.mkdir(parents=True, exist_ok=True)
        self.leases[token] = LeaseState(
            token=token,
            lease_id=lease_id,
            task_id=str(payload.get("task_id") or ""),
            intent_id=str(payload.get("intent_id") or ""),
            run_dir=run_dir,
            scope=dict(payload.get("scope") or {}),
            cdp_url=str(payload.get("cdp_url") or "http://127.0.0.1:9223"),
        )
        return self._ok("Browser Lease 已注册")

    async def _close_lease(self, token: str) -> None:
        lease = self.leases.pop(token, None)
        if lease is None:
            return
        for page_state in list(lease.pages.values()):
            with contextlib.suppress(Exception):
                await page_state.page.close()
            await self._clear_refs(page_state)
        lease.pages.clear()
        # 当所有活跃 Lease 均已关闭（空闲期/任务暂停）且 Chrome 归 Broker 所有时，
        # 自动断开并平滑退出无头 Chrome 实例，释放 SingletonLock 文件锁，
        # 方便操作者随时点击系统中的 Chrome-SecTest 浏览器进行手动登录与凭证维护。
        if not self.leases and self._chrome_pid is not None:
            async with self._connect_lock:
                if self._browser is not None:
                    with contextlib.suppress(Exception):
                        await self._browser.close()
                    self._browser = None
                    self._context = None
                if self._playwright is not None:
                    with contextlib.suppress(Exception):
                        await self._playwright.stop()
                    self._playwright = None
                self._stop_owned_chrome()

    async def _tool(
        self, lease: LeaseState, operation: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        handlers = {
            "browser_open_page": self._open_page,
            "browser_list_pages": self._list_pages,
            "browser_close_page": self._close_page,
            "browser_navigate": self._navigate,
            "browser_read_page": self._read_page_tool,
            "browser_snapshot": self._snapshot_tool,
            "browser_click": self._click,
            "browser_fill": self._fill,
            "browser_press": self._press,
            "browser_hover": self._hover,
            "browser_scroll": self._scroll,
            "browser_select": self._select,
            "browser_check": self._check,
            "browser_upload_file": self._upload_file,
            "browser_wait": self._wait,
            "browser_screenshot": self._screenshot,
            "browser_read_network": self._read_network,
            "browser_read_console": self._read_console,
            "browser_evaluate": self._evaluate,
            "browser_get_credentials": self._get_credentials,
        }
        handler = handlers.get(operation)
        if handler is None:
            raise BrowserBrokerError("unknown_operation", f"Unknown browser tool: {operation}")
        return await handler(lease, params)

    async def _ensure_browser(self, lease: LeaseState) -> BrowserContext:
        async with self._connect_lock:
            if self._browser is not None and self._browser.is_connected():
                if self._cdp_url != lease.cdp_url:
                    raise BrowserBrokerError(
                        "cdp_mismatch",
                        "同一 Runner Browser Broker不能连接多个CDP地址",
                    )
                assert self._context is not None
                return self._context
            if self._playwright is None:
                self._playwright = await async_playwright().start()
            # Always verify ownership before attaching.  A live CDP endpoint may
            # belong to the user's ordinary browser; attaching to it would leak
            # pages and credentials across Tasks.  _launch_chrome is idempotent
            # for the Broker-owned profile and starts Chrome only when needed.
            await self._launch_chrome(lease)
            try:
                self._browser = await self._playwright.chromium.connect_over_cdp(
                    lease.cdp_url, timeout=10_000
                )
            except Exception as exc:
                raise BrowserBrokerError(
                    "browser_unavailable",
                    f"无法连接Chrome-SecTest：{exc}",
                    retryable=True,
                ) from exc
            if not self._browser.contexts:
                raise BrowserBrokerError("browser_context_missing", "Chrome没有可用BrowserContext")
            self._context = self._browser.contexts[0]
            self._cdp_url = lease.cdp_url
            await self._rebind_pages()
            await self._minimize_only_if_already_minimized()
            return self._context

    @staticmethod
    def _chrome_profile_dir() -> Path:
        configured = os.environ.get("AXIOM_CHROME_PROFILE_DIR", "").strip()
        if configured:
            return Path(configured).expanduser().resolve()
        if platform.system() == "Darwin":
            return (Path.home() / "Library/Application Support/Chrome-SecTest").resolve()
        return (Path.home() / ".config/Chrome-SecTest").resolve()

    @staticmethod
    def _chrome_binary() -> str | None:
        configured = os.environ.get("CHROME_BIN", "").strip()
        candidates = [
            configured,
            # 优先使用 Playwright 自带的独立 Google Chrome for Testing，实现进程与内存空间的物理级隔离
            str(Path.home() / "Library/Caches/ms-playwright/chromium-1228/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"),
            str(Path.home() / "Library/Caches/ms-playwright/chromium-1223/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"),
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "google-chrome",
            "google-chrome-stable",
            "chromium",
            "chromium-browser",
            "chrome",
        ]
        for candidate in candidates:
            if not candidate:
                continue
            path = Path(candidate).expanduser()
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
            resolved = shutil.which(candidate)
            if resolved:
                return resolved
        return None

    @staticmethod
    def _cdp_version_url(cdp_url: str) -> str:
        parsed = urlparse(cdp_url)
        return urlunparse((parsed.scheme or "http", parsed.netloc, "/json/version", "", "", ""))

    @classmethod
    def _cdp_alive(cls, cdp_url: str) -> bool:
        import urllib.request
        try:
            # 强制不使用本地 HTTP 代理探测 127.0.0.1
            handler = urllib.request.ProxyHandler({})
            opener = urllib.request.build_opener(handler)
            req = urllib.request.Request(cls._cdp_version_url(cdp_url), headers={"Connection": "close"})
            with opener.open(req, timeout=1.0) as response:
                return 200 <= int(response.status) < 500
        except Exception:
            return False

    @staticmethod
    def _port_is_open(host: str, port: int) -> bool:
        try:
            with socket.create_connection((host, port), timeout=0.25):
                return True
        except OSError:
            return False

    @staticmethod
    def _process_command(pid: int) -> str:
        try:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="],
                check=False,
                capture_output=True,
                text=True,
                timeout=1.0,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return result.stdout.strip()

    @classmethod
    def _is_expected_chrome_command(cls, pid: int, *, port: int, profile: Path) -> bool:
        command = cls._process_command(pid)
        return bool(
            command
            and "--type=" not in command
            and f"--remote-debugging-port={port}" in command
            and ("Chrome-SecTest" in command or f"--user-data-dir={profile}" in command)
        )

    @classmethod
    def _find_expected_chrome_pid(cls, *, port: int, profile: Path) -> int | None:
        try:
            result = subprocess.run(
                ["ps", "ax", "-o", "pid=", "-o", "command="],
                check=False,
                capture_output=True,
                text=True,
                timeout=2.0,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        for line in result.stdout.splitlines():
            parts = line.strip().split(None, 1)
            if not parts:
                continue
            try:
                pid = int(parts[0])
            except ValueError:
                continue
            if cls._is_expected_chrome_command(pid, port=port, profile=profile):
                return pid
        return None

    def _stop_owned_chrome(self) -> None:
        pid = self._chrome_pid
        if pid is None:
            return
        profile = self._chrome_profile or self._chrome_profile_dir()
        port = self._chrome_port or urlparse(self._cdp_url).port or 9223
        # 严格安全断言：必须精确匹配包含当前 Chrome-SecTest 独立 Profile 和端口的命令，严禁触碰任何常规浏览器实例
        if self._is_expected_chrome_command(pid, port=port, profile=profile):
            try:
                pgid_result = subprocess.run(
                    ["ps", "-p", str(pid), "-o", "pgid="],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=1.0,
                )
                pgid_text = pgid_result.stdout.strip()
                pgid = int(pgid_text) if pgid_text.isdigit() else pid
                # 二次校验 pgid 领头进程是否仍属于受控 Chrome，防止系统 PID 复用误杀
                if pgid != os.getpid() and self._is_expected_chrome_command(pgid, port=port, profile=profile):
                    os.killpg(pgid, signal.SIGTERM)
                else:
                    os.kill(pid, signal.SIGTERM)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
        else:
            # 记录警告，拒绝发送信号
            pass
        if self._chrome_pid_file is not None:
            self._chrome_pid_file.unlink(missing_ok=True)
        self._chrome_pid = None
        self._chrome_port = None
        self._chrome_profile = None
        self._chrome_pid_file = None

    async def _launch_chrome(self, lease: LeaseState) -> None:
        parsed_cdp = urlparse(lease.cdp_url)
        host = (parsed_cdp.hostname or "127.0.0.1").lower()
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise BrowserBrokerError(
                "browser_cdp_invalid",
                "Browser Broker 只允许连接本机 Chrome CDP",
            )
        port = parsed_cdp.port or 9223
        profile = self._chrome_profile_dir()
        pid_file = Path(tempfile.gettempdir()) / f"chrome-sectest-{port}.pid"
        if self._cdp_alive(lease.cdp_url):
            expected_pid = self._find_expected_chrome_pid(port=port, profile=profile)
            if expected_pid is None:
                raise BrowserBrokerError(
                    "browser_cdp_port_occupied",
                    f"CDP 端口 {port} 已被非 Chrome-SecTest 进程占用，拒绝接管",
                    retryable=True,
                )
            # This Broker did not start the already-running browser, so keep
            # it attached but do not claim ownership or terminate it on close.
            return
        # A non-CDP service can still occupy the configured port.  Do not
        # start Chrome and let it fail ambiguously; report the ownership issue
        # explicitly so the caller can choose another port or stop that service.
        if self._port_is_open(host, port):
            raise BrowserBrokerError(
                "browser_cdp_port_occupied",
                f"CDP 端口 {port} 已被其他进程占用，拒绝接管",
                retryable=True,
            )
        binary = self._chrome_binary()
        if binary is None:
            raise BrowserBrokerError(
                "browser_launcher_missing",
                "未找到 Chrome/Chromium；请配置 CHROME_BIN",
                retryable=True,
            )
        profile.mkdir(parents=True, exist_ok=True)
        # Apple Silicon 上强制以 arm64 原生运行 Chrome：
        # 若进程树被 Rosetta (x86_64 转译) 拉起，JS 执行全走翻译层，
        # 高频动画页面 CPU 会飙到 100%+ 且严重掉帧（实测 108% vs 原生 14%）。
        launcher = (
            ["arch", "-arm64"]
            if platform.system() == "Darwin" and platform.machine() == "arm64"
            else []
        )
        args = [
            *launcher,
            binary,
            f"--user-data-dir={profile}",
            f"--remote-debugging-port={port}",
            "--remote-allow-origins=*",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-background-timer-throttling",
            "--enable-gpu-rasterization",
            "--enable-zero-copy",
            "--ignore-gpu-blocklist",
            "--autoplay-policy=user-gesture-required",
            "--no-first-run",
            "--no-default-browser-check",
            "--window-size=1280,800",
            "--ignore-certificate-errors",
            "--allow-insecure-localhost",
        ]
        headless_mode = os.environ.get("CHROME_HEADLESS", "1")
        if headless_mode in {"1", "new"}:
            args.append("--headless=new")
        elif headless_mode in {"old", "legacy"}:
            args.append("--headless")
        elif os.environ.get("CHROME_WINDOW") == "minimized":
            args.append("--start-minimized")
        environment = {
            key: value
            for key, value in os.environ.items()
            if not any(marker in key.upper() for marker in ("API_KEY", "TOKEN", "SECRET"))
        }
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=environment,
                start_new_session=True,
            )
        except OSError as exc:
            raise BrowserBrokerError(
                "browser_start_failed", f"启动 Chrome-SecTest 失败：{exc}", retryable=True
            ) from exc
        self._chrome_pid = process.pid
        self._chrome_port = port
        self._chrome_profile = profile
        self._chrome_pid_file = pid_file
        pid_file.write_text(str(process.pid), encoding="ascii")
        for _ in range(20):
            await asyncio.sleep(0.5)
            if not self._cdp_alive(lease.cdp_url):
                continue
            ready_pid = self._find_expected_chrome_pid(port=port, profile=profile)
            if ready_pid is not None:
                self._chrome_pid = ready_pid
                pid_file.write_text(str(ready_pid), encoding="ascii")
                return
        self._stop_owned_chrome()
        raise BrowserBrokerError(
            "browser_start_timeout", "Chrome-SecTest 启动超时", retryable=True
        )

    async def _rebind_pages(self) -> None:
        if self._context is None:
            return
        live: dict[str, Page] = {}
        for page in self._context.pages:
            with contextlib.suppress(Exception):
                live[await self._target_id(page)] = page
        for lease in self.leases.values():
            for page_id, state in list(lease.pages.items()):
                replacement = live.get(state.target_id)
                if replacement is None:
                    lease.pages.pop(page_id, None)
                    continue
                state.page = replacement
                state.generation += 1
                await self._clear_refs(state)
                self._wire_page(lease, state)

    async def _open_page(
        self, lease: LeaseState, params: dict[str, Any]
    ) -> dict[str, Any]:
        url = str(params.get("url") or "about:blank")
        self._require_target(lease, url)
        context = await self._ensure_browser(lease)
        was_minimized = await self._all_windows_minimized()
        page = await context.new_page()
        state = await self._register_page(lease, page)
        wait_until = str(params.get("wait_until") or "domcontentloaded")
        navigation_note = ""
        if url != "about:blank":
            try:
                await page.goto(
                    url,
                    wait_until=wait_until,
                    timeout=self._timeout_ms(params),
                )
            except Exception as exc:
                navigation_note = str(exc)
        if was_minimized:
            # new_page() 会把 Chrome 窗口激活到前台;若之前所有窗口都是
            # 最小化状态(用户没有人工恢复),立刻压回,避免抢占前台焦点。
            await self._minimize_all_windows()
        return await self._page_response(
            state,
            f"已打开页面 {state.page_id}",
            extra={"navigation_note": navigation_note} if navigation_note else None,
        )

    async def _list_pages(
        self, lease: LeaseState, _params: dict[str, Any]
    ) -> dict[str, Any]:
        pages: list[dict[str, Any]] = []
        for page_id, state in list(lease.pages.items()):
            if state.page.is_closed():
                lease.pages.pop(page_id, None)
                continue
            pages.append(await self._page_data(state))
        content = "\n".join(
            f"{item['page_id']} {item['url']} {item['title']}" for item in pages
        ) or "当前Browser Lease没有页面"
        return self._ok(content, {"pages": pages})

    async def _close_page(
        self, lease: LeaseState, params: dict[str, Any]
    ) -> dict[str, Any]:
        state = self._page(lease, params)
        page_id = state.page_id
        # 安全防御：只允许关闭本 Lease 登记并由当前任务创建的 page，拒绝关闭外部或用户未授权关联的页面
        if page_id not in lease.pages:
            raise BrowserBrokerError("page_not_owned", f"页面 {page_id} 不属于当前会话，拒绝关闭")
        with contextlib.suppress(Exception):
            await state.page.close()
        await self._clear_refs(state)
        lease.pages.pop(page_id, None)
        return self._ok(f"已关闭页面 {page_id}", {"page_id": page_id})

    async def _navigate(
        self, lease: LeaseState, params: dict[str, Any]
    ) -> dict[str, Any]:
        state = self._page(lease, params)
        url = str(params.get("url") or "")
        self._require_target(lease, url)
        wait_until = str(params.get("wait_until") or "domcontentloaded")
        async with state.lock:
            try:
                await state.page.goto(url, wait_until=wait_until, timeout=self._timeout_ms(params))
            except Exception as exc:
                return await self._page_response(
                    state,
                    f"导航返回提示：{exc}",
                    extra={"navigation_note": str(exc)},
                )
        return await self._page_response(state, f"已导航到 {state.page.url}")

    async def _read_page_tool(
        self, lease: LeaseState, params: dict[str, Any]
    ) -> dict[str, Any]:
        state = self._page(lease, params)
        scope = str(params.get("scope") or "viewport")
        max_chars = min(max(int(params.get("max_chars") or 20_000), 1000), MAX_RESPONSE_CHARS)
        content, truncated = await self._rendered_text(state.page, scope=scope, max_chars=max_chars)
        data = await self._page_data(state)
        data.update({"scope": scope, "truncated": truncated})
        return self._ok(content or "页面没有可读文本", data)

    async def _snapshot_tool(
        self, lease: LeaseState, params: dict[str, Any]
    ) -> dict[str, Any]:
        state = self._page(lease, params)
        scope = str(params.get("scope") or "interactive")
        if scope not in {"interactive", "viewport", "subtree", "full", "changes"}:
            raise BrowserBrokerError("invalid_scope", f"不支持的Snapshot scope：{scope}")
        ancestor = None
        if scope == "subtree":
            ancestor = self._ref(state, str(params.get("ref") or ""), action="scroll")
        async with state.lock:
            records = await self._collect_refs(state, scope=scope, ancestor=ancestor)
            lines = [self._format_ref(item) for item in records]
            effective_scope = "interactive" if scope == "changes" else scope
            output_lines = lines
            if scope == "changes":
                previous = state.snapshot_lines.get("interactive", [])
                added = [line for line in lines if line not in previous]
                removed = [line for line in previous if line not in lines]
                output_lines = [*(f"+ {line}" for line in added), *(f"- {line}" for line in removed)]
                if not output_lines:
                    output_lines = ["无结构变化"]
            if effective_scope in {"viewport", "full"}:
                text, _ = await self._rendered_text(
                    state.page,
                    scope=effective_scope,
                    max_chars=12_000,
                )
                if text:
                    output_lines = ["页面文本：", text, "交互结构：", *output_lines]
            state.snapshot_lines[effective_scope] = lines
            state.snapshot_sequence += 1
            snapshot_id = f"s{state.snapshot_sequence:04d}"
            joined = "\n".join(output_lines)
            truncated = len(joined) > MAX_RESPONSE_CHARS or len(records) >= MAX_SNAPSHOT_REFS
            joined = joined[:MAX_RESPONSE_CHARS]
        page_data = await self._page_data(state)
        page_data.update(
            {
                "snapshot_id": snapshot_id,
                "scope": scope,
                "truncated": truncated,
                "refs": [self._public_ref(item) for item in records],
                "limitations": ["closed_shadow_root"],
            }
        )
        header = (
            f"PAGE {state.page_id} generation={state.generation} snapshot={snapshot_id} "
            f"url={state.page.url}\n"
        )
        return self._ok(header + (joined or "没有匹配的页面节点"), page_data)

    async def _click(self, lease: LeaseState, params: dict[str, Any]) -> dict[str, Any]:
        return await self._element_action(lease, params, "click")

    async def _fill(self, lease: LeaseState, params: dict[str, Any]) -> dict[str, Any]:
        return await self._element_action(lease, params, "fill", value=str(params.get("value") or ""))

    async def _hover(self, lease: LeaseState, params: dict[str, Any]) -> dict[str, Any]:
        return await self._element_action(lease, params, "hover")

    async def _select(self, lease: LeaseState, params: dict[str, Any]) -> dict[str, Any]:
        return await self._element_action(lease, params, "select", value=str(params.get("value") or ""))

    async def _check(self, lease: LeaseState, params: dict[str, Any]) -> dict[str, Any]:
        return await self._element_action(lease, params, "check", value=bool(params.get("checked", True)))

    async def _element_action(
        self,
        lease: LeaseState,
        params: dict[str, Any],
        action: str,
        *,
        value: Any = None,
    ) -> dict[str, Any]:
        state = self._page(lease, params)
        ref = self._ref(state, str(params.get("ref") or ""), action=action)
        before = set(lease.pages)
        console_before = state.console_cursor
        download_before = state.download_cursor
        async with state.lock:
            await self._require_live_ref(ref)
            timeout = self._timeout_ms(params)
            if action == "click":
                await ref.handle.click(timeout=timeout)
            elif action == "fill":
                await ref.handle.fill(str(value), timeout=timeout)
            elif action == "hover":
                await ref.handle.hover(timeout=timeout)
            elif action == "select":
                selected = await ref.handle.select_option(value=str(value), timeout=timeout)
                if not selected:
                    selected = await ref.handle.select_option(label=str(value), timeout=timeout)
                if not selected:
                    raise BrowserBrokerError("option_not_found", f"下拉选项不存在：{value}")
            elif action == "check":
                if bool(value):
                    await ref.handle.check(timeout=timeout)
                else:
                    await ref.handle.uncheck(timeout=timeout)
            await asyncio.sleep(0.05)
        extra = {
            "ref": ref.ref,
            "action": action,
            "new_page_ids": sorted(set(lease.pages) - before),
            "dialogs": [
                item
                for item in state.console_entries
                if int(item.get("cursor") or 0) > console_before
                and item.get("kind") == "dialog"
            ],
            "downloads": [
                item
                for item in state.download_entries
                if int(item.get("cursor") or 0) > download_before
            ],
            "changes": await self._change_summary(state),
        }
        return await self._page_response(state, f"{action} 已执行：{ref.ref}", extra=extra)

    async def _press(self, lease: LeaseState, params: dict[str, Any]) -> dict[str, Any]:
        state = self._page(lease, params)
        key = str(params.get("key") or "")
        if not key:
            raise BrowserBrokerError("invalid_key", "key不能为空")
        ref = self._ref(state, str(params.get("ref") or ""), action="press")
        async with state.lock:
            await self._require_live_ref(ref)
            await ref.handle.press(key, timeout=self._timeout_ms(params))
        return await self._page_response(state, f"已发送按键 {key}")

    async def _scroll(self, lease: LeaseState, params: dict[str, Any]) -> dict[str, Any]:
        state = self._page(lease, params)
        ref_value = str(params.get("ref") or "")
        async with state.lock:
            if ref_value:
                ref = self._ref(state, ref_value, action="scroll")
                await self._require_live_ref(ref)
                await ref.handle.scroll_into_view_if_needed(timeout=self._timeout_ms(params))
            else:
                delta_x = float(params.get("delta_x") or 0)
                delta_y = float(params.get("delta_y") or 0)
                if delta_x == 0 and delta_y == 0:
                    raise BrowserBrokerError("invalid_scroll", "页面滚动需要非零delta")
                await state.page.mouse.wheel(delta_x, delta_y)
        return await self._page_response(state, "滚动已执行")

    async def _upload_file(
        self, lease: LeaseState, params: dict[str, Any]
    ) -> dict[str, Any]:
        state = self._page(lease, params)
        ref = self._ref(state, str(params.get("ref") or ""), action="upload")
        paths = params.get("file_paths")
        if not isinstance(paths, list) or not paths:
            raise BrowserBrokerError("invalid_files", "file_paths不能为空")
        resolved = [str(self._inside_run_dir(lease, str(item))) for item in paths]
        await self._require_live_ref(ref)
        await ref.handle.set_input_files(resolved, timeout=self._timeout_ms(params))
        return await self._page_response(state, f"已上传 {len(resolved)} 个文件")

    async def _wait(self, lease: LeaseState, params: dict[str, Any]) -> dict[str, Any]:
        state = self._page(lease, params)
        condition = str(params.get("condition") or "")
        timeout = self._timeout_ms(params)
        value = str(params.get("value") or "")
        if condition == "text":
            await state.page.get_by_text(value, exact=False).first.wait_for(state="visible", timeout=timeout)
        elif condition == "url":
            pattern = value if "*" in value else f"**{value}**"
            await state.page.wait_for_url(pattern, timeout=timeout)
        elif condition == "load_state":
            await state.page.wait_for_load_state(str(params.get("state") or "domcontentloaded"), timeout=timeout)
        elif condition == "ref":
            ref = self._ref(state, value, action="scroll")
            await self._require_live_ref(ref)
        elif condition == "time":
            duration = min(max(int(params.get("duration_ms") or 0), 0), 30_000)
            await asyncio.sleep(duration / 1000)
        elif condition == "download":
            baseline = int(params.get("after_cursor") or state.download_cursor)
            deadline = time.monotonic() + timeout / 1000
            while state.download_cursor <= baseline and time.monotonic() < deadline:
                await asyncio.sleep(0.1)
            if state.download_cursor <= baseline:
                raise BrowserBrokerError("download_timeout", "等待下载超时", retryable=True)
        else:
            raise BrowserBrokerError("invalid_wait_condition", f"未知等待条件：{condition}")
        extra: dict[str, Any] = {"condition": condition}
        if condition == "download" and state.download_entries:
            extra["download"] = state.download_entries[-1]
        return await self._page_response(state, f"等待条件已满足：{condition}", extra=extra)

    async def _screenshot(
        self, lease: LeaseState, params: dict[str, Any]
    ) -> dict[str, Any]:
        state = self._page(lease, params)
        output = str(params.get("output_path") or "")
        if output:
            path = self._inside_run_dir(lease, output)
        else:
            path = lease.run_dir / "process" / "browser" / "screenshots" / f"{int(time.time() * 1000)}-{state.page_id}.png"
        if path.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
            raise BrowserBrokerError("invalid_screenshot_format", "截图仅支持PNG或JPEG")
        path.parent.mkdir(parents=True, exist_ok=True)
        labels = bool(params.get("labels", False))
        overlay_id = f"axiom-labels-{uuid.uuid4().hex}"
        label_to_ref: dict[str, str] = {}
        if labels:
            markers: list[dict[str, Any]] = []
            for ref in list(state.refs.values())[:100]:
                with contextlib.suppress(Exception):
                    box = await ref.handle.bounding_box()
                    if box:
                        label_to_ref[ref.ref] = ref.ref
                        markers.append({"ref": ref.ref, **box})
            await state.page.evaluate(
                """({id, markers}) => {
                  const root = document.createElement('div'); root.id = id;
                  for (const item of markers) {
                    const tag = document.createElement('div'); tag.textContent = item.ref;
                    Object.assign(tag.style, {position:'absolute',left:`${item.x + window.scrollX}px`,top:`${item.y + window.scrollY}px`,zIndex:'2147483647',background:'#f59e0b',color:'#111827',font:'bold 11px monospace',padding:'1px 3px',border:'1px solid #111827',borderRadius:'3px',pointerEvents:'none'});
                    root.appendChild(tag);
                  }
                  document.documentElement.appendChild(root);
                }""",
                {"id": overlay_id, "markers": markers},
            )
        try:
            await state.page.screenshot(
                path=str(path),
                full_page=bool(params.get("full_page", False)),
                quality=(int(params["quality"]) if path.suffix.lower() in {".jpg", ".jpeg"} and params.get("quality") else None),
            )
        finally:
            if labels:
                with contextlib.suppress(Exception):
                    await state.page.evaluate("id => document.getElementById(id)?.remove()", overlay_id)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        data = await self._page_data(state)
        data.update(
            {
                "output_path": str(path),
                "size": path.stat().st_size,
                "sha256": digest,
                "label_to_ref": label_to_ref,
            }
        )
        return self._ok(f"截图已保存：{path}", data)

    async def _read_network(
        self, lease: LeaseState, params: dict[str, Any]
    ) -> dict[str, Any]:
        state = self._page(lease, params)
        after = max(int(params.get("after_cursor") or 0), 0)
        limit = min(max(int(params.get("limit") or 100), 1), 500)
        contains = str(params.get("url_contains") or "")
        include_sensitive = bool(params.get("include_sensitive", False))
        rows = [
            self._network_public(item, include_sensitive=include_sensitive)
            for item in state.network_entries
            if int(item.get("cursor") or 0) > after
            and (not contains or contains in str(item.get("url") or ""))
        ][:limit]
        content = "\n".join(json.dumps(item, ensure_ascii=False) for item in rows) or "没有新的网络记录"
        return self._ok(
            content[:MAX_RESPONSE_CHARS],
            {
                "page_id": state.page_id,
                "entries": rows,
                "next_cursor": rows[-1]["cursor"] if rows else after,
                "latest_cursor": state.network_cursor,
            },
        )

    async def _read_console(
        self, lease: LeaseState, params: dict[str, Any]
    ) -> dict[str, Any]:
        state = self._page(lease, params)
        after = max(int(params.get("after_cursor") or 0), 0)
        limit = min(max(int(params.get("limit") or 100), 1), 500)
        rows = [item for item in state.console_entries if int(item.get("cursor") or 0) > after][:limit]
        content = "\n".join(json.dumps(item, ensure_ascii=False) for item in rows) or "没有新的Console记录"
        return self._ok(
            content[:MAX_RESPONSE_CHARS],
            {
                "page_id": state.page_id,
                "entries": rows,
                "next_cursor": rows[-1]["cursor"] if rows else after,
                "latest_cursor": state.console_cursor,
            },
        )

    async def _evaluate(
        self, lease: LeaseState, params: dict[str, Any]
    ) -> dict[str, Any]:
        state = self._page(lease, params)
        expression = str(params.get("expression") or "")
        if not expression:
            raise BrowserBrokerError("invalid_expression", "expression不能为空")
        value = await state.page.evaluate(expression, params.get("argument"))
        try:
            rendered = json.dumps(value, ensure_ascii=False, default=str)
        except TypeError:
            rendered = str(value)
        data = await self._page_data(state)
        data["value"] = value
        return self._ok(rendered[:MAX_RESPONSE_CHARS], data)

    async def _get_credentials(
        self, lease: LeaseState, params: dict[str, Any]
    ) -> dict[str, Any]:
        context = await self._ensure_browser(lease)
        domain_query = str(params.get("domain") or "").strip().lower()
        page_id = str(params.get("page_id") or "").strip()
        include_storage = bool(params.get("include_storage", True))
        include_expired = bool(params.get("include_expired", False))

        target_host = ""
        if page_id:
            state = self._page(lease, {"page_id": page_id})
            target_host = urlparse(state.page.url).hostname or ""
        elif domain_query:
            if "://" in domain_query:
                target_host = urlparse(domain_query).hostname or ""
            else:
                target_host = domain_query.lstrip("*.")

        if not target_host and not domain_query:
            # 如果既未传 page_id 也未传 domain，尝试使用 lease 内第一个打开页面的 host
            for st in lease.pages.values():
                h = urlparse(st.page.url).hostname
                if h:
                    target_host = h
                    break

        if not target_host and domain_query:
            target_host = domain_query.lstrip("*.")

        # 1. 提取所有 Cookie
        all_cookies = await context.cookies()
        matched_cookies = []
        now = time.time()

        for c in all_cookies:
            c_domain = str(c.get("domain") or "").lower().lstrip(".")
            if not include_expired:
                exp = c.get("expires", -1)
                if 0 < exp < now:
                    continue

            if target_host:
                if c_domain == target_host or target_host.endswith("." + c_domain) or c_domain.endswith("." + target_host):
                    matched_cookies.append(c)
            else:
                matched_cookies.append(c)

        # 2. 构造格式化请求头
        cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in matched_cookies)
        formatted_headers = {}
        if cookie_header:
            formatted_headers["Cookie"] = cookie_header

        # 3. 提取相关页面的 Storage
        storage_by_origin: dict[str, Any] = {}
        if include_storage and lease.pages:
            for st in lease.pages.values():
                try:
                    p_url = st.page.url
                    p_host = urlparse(p_url).hostname or ""
                    if target_host and not (p_host == target_host or p_host.endswith("." + target_host) or target_host.endswith("." + p_host)):
                        continue
                    origin = f"{urlparse(p_url).scheme}://{urlparse(p_url).netloc}" if "://" in p_url else p_url
                    storage_res = await st.page.evaluate(
                        "() => ({localStorage: Object.fromEntries(Object.entries(localStorage)), sessionStorage: Object.fromEntries(Object.entries(sessionStorage))})"
                    )
                    if isinstance(storage_res, dict):
                        storage_by_origin[origin] = storage_res
                        # 如果 storage 里有 token 类字段，辅助填充 Authorization
                        ls = storage_res.get("localStorage") or {}
                        for k, v in ls.items():
                            if isinstance(v, str) and ("token" in k.lower() or "auth" in k.lower()):
                                if v.lower().startswith("bearer "):
                                    formatted_headers["Authorization"] = v
                                elif len(v) > 20 and not formatted_headers.get("Authorization"):
                                    formatted_headers["Authorization"] = f"Bearer {v}"
                except Exception:
                    pass

        # 4. 组装展示文本
        lines = [
            f"### 凭证提取结果 (目标: {target_host or '全量域'})",
            f"- **匹配 Cookie 数**: {len(matched_cookies)} (HttpOnly: {sum(1 for c in matched_cookies if c.get('httpOnly'))})",
            f"- **关联 Storage Origin 数**: {len(storage_by_origin)}",
        ]
        if formatted_headers:
            lines.append("\n**Formatted Headers:**")
            for hk, hv in formatted_headers.items():
                lines.append(f"- `{hk}`: `{hv[:200] + '...' if len(hv) > 200 else hv}`")

        if matched_cookies:
            lines.append("\n**Cookies 详情:**")
            for c in matched_cookies:
                http_only_tag = " [HttpOnly]" if c.get("httpOnly") else ""
                lines.append(f"- `{c.get('name')}`: `{c.get('value')}` (domain: `{c.get('domain')}`){http_only_tag}")

        rendered = "\n".join(lines)
        data = {
            "target": target_host or domain_query or "all",
            "cookie_count": len(matched_cookies),
            "cookies": matched_cookies,
            "storage_by_origin": storage_by_origin,
            "formatted_headers": formatted_headers,
        }
        return self._ok(rendered[:MAX_RESPONSE_CHARS], data)

    async def _register_page(self, lease: LeaseState, page: Page) -> PageState:
        lease.page_sequence += 1
        page_id = f"p{lease.page_sequence:03d}"
        state = PageState(page_id=page_id, page=page, target_id=await self._target_id(page))
        lease.pages[page_id] = state
        self._wire_page(lease, state)
        return state

    def _wire_page(self, lease: LeaseState, state: PageState) -> None:
        page = state.page

        def task(coro) -> None:
            asyncio.create_task(coro)

        page.on("framenavigated", lambda frame: task(self._frame_navigated(state, frame)))
        page.on("popup", lambda popup: task(self._popup(lease, popup)))
        page.on("request", lambda request: task(self._request_event(lease, state, request)))
        page.on("response", lambda response: task(self._response_event(lease, state, response)))
        page.on("console", lambda message: self._console_event(lease, state, "console", message.type, message.text))
        page.on("pageerror", lambda error: self._console_event(lease, state, "pageerror", "error", str(error)))
        page.on("dialog", lambda dialog: task(self._dialog_event(lease, state, dialog)))
        page.on("download", lambda download: task(self._download_event(lease, state, download)))
        page.on("websocket", lambda websocket: self._websocket_event(lease, state, websocket))

    async def _frame_navigated(self, state: PageState, frame: Frame) -> None:
        if frame == state.page.main_frame:
            state.generation += 1
            state.snapshot_lines.clear()
            await self._clear_refs(state)

    async def _popup(self, lease: LeaseState, popup: Page) -> None:
        if any(item.page == popup for item in lease.pages.values()):
            return
        await self._register_page(lease, popup)

    async def _request_event(self, lease: LeaseState, state: PageState, request) -> None:
        headers: dict[str, str] = {}
        with contextlib.suppress(Exception):
            headers = await request.all_headers()
        entry = {
            "kind": "request",
            "page_id": state.page_id,
            "time": time.time(),
            "method": request.method,
            "url": request.url,
            "resource_type": request.resource_type,
            "headers": headers,
            "post_data": (request.post_data or "")[:1_000_000],
        }
        self._append_network(lease, state, entry)

    async def _response_event(self, lease: LeaseState, state: PageState, response) -> None:
        headers: dict[str, str] = {}
        with contextlib.suppress(Exception):
            headers = await response.all_headers()
        entry: dict[str, Any] = {
            "kind": "response",
            "page_id": state.page_id,
            "time": time.time(),
            "url": response.url,
            "status": response.status,
            "headers": headers,
        }
        content_type = str(headers.get("content-type") or "").lower()
        if any(marker in content_type for marker in ("json", "text", "javascript", "xml")):
            with contextlib.suppress(Exception):
                body = await response.body()
                entry["body"] = body[:1_000_000].decode("utf-8", errors="replace")
                entry["body_truncated"] = len(body) > 1_000_000
        self._append_network(lease, state, entry)

    def _websocket_event(self, lease: LeaseState, state: PageState, websocket) -> None:
        self._append_network(
            lease,
            state,
            {
                "kind": "websocket_open",
                "page_id": state.page_id,
                "time": time.time(),
                "url": websocket.url,
            },
        )
        websocket.on(
            "framesent",
            lambda payload: self._append_network(
                lease,
                state,
                {
                    "kind": "websocket_sent",
                    "page_id": state.page_id,
                    "time": time.time(),
                    "url": websocket.url,
                    "payload": str(payload)[:100_000],
                },
            ),
        )
        websocket.on(
            "framereceived",
            lambda payload: self._append_network(
                lease,
                state,
                {
                    "kind": "websocket_received",
                    "page_id": state.page_id,
                    "time": time.time(),
                    "url": websocket.url,
                    "payload": str(payload)[:100_000],
                },
            ),
        )
        websocket.on(
            "close",
            lambda: self._append_network(
                lease,
                state,
                {
                    "kind": "websocket_close",
                    "page_id": state.page_id,
                    "time": time.time(),
                    "url": websocket.url,
                },
            ),
        )

    def _console_event(
        self,
        lease: LeaseState,
        state: PageState,
        kind: str,
        level: str,
        text: str,
    ) -> None:
        state.console_cursor += 1
        entry = {
            "cursor": state.console_cursor,
            "page_id": state.page_id,
            "kind": kind,
            "level": level,
            "text": text[:100_000],
            "time": time.time(),
        }
        state.console_entries.append(entry)
        if len(state.console_entries) > 5_000:
            del state.console_entries[:1_000]
        self._append_jsonl(lease, "console.jsonl", entry)

    async def _dialog_event(self, lease: LeaseState, state: PageState, dialog) -> None:
        self._console_event(lease, state, "dialog", dialog.type, dialog.message)
        with contextlib.suppress(Exception):
            await dialog.dismiss()

    async def _download_event(self, lease: LeaseState, state: PageState, download) -> None:
        directory = lease.run_dir / "process" / "browser" / "downloads"
        directory.mkdir(parents=True, exist_ok=True)
        name = Path(download.suggested_filename or f"download-{int(time.time() * 1000)}").name
        path = directory / name
        with contextlib.suppress(Exception):
            await download.save_as(str(path))
        state.download_cursor += 1
        entry = {
            "cursor": state.download_cursor,
            "page_id": state.page_id,
            "time": time.time(),
            "suggested_filename": name,
            "path": str(path),
        }
        state.download_entries.append(entry)
        self._append_jsonl(lease, "downloads.jsonl", entry)

    def _append_network(self, lease: LeaseState, state: PageState, entry: dict[str, Any]) -> None:
        state.network_cursor += 1
        record = {"cursor": state.network_cursor, **entry}
        state.network_entries.append(record)
        if len(state.network_entries) > 10_000:
            del state.network_entries[:2_000]
        self._append_jsonl(lease, "network.jsonl", record)

    def _append_jsonl(self, lease: LeaseState, name: str, entry: dict[str, Any]) -> None:
        directory = lease.run_dir / "process" / "browser"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        with contextlib.suppress(OSError):
            path.chmod(0o600)

    async def _collect_refs(
        self,
        state: PageState,
        *,
        scope: str,
        ancestor: RefState | None,
    ) -> list[RefState]:
        records: list[RefState] = []
        viewport = await state.page.evaluate("() => ({width: innerWidth, height: innerHeight})")
        live_dom_keys: set[str] = set()
        for frame in state.page.frames:
            frame_id = self._frame_id(state, frame)
            locator = frame.locator(INTERACTIVE_SELECTOR)
            with contextlib.suppress(Exception):
                count = min(await locator.count(), MAX_SNAPSHOT_REFS - len(records))
                for index in range(count):
                    handle = await locator.nth(index).element_handle()
                    if handle is None:
                        continue
                    try:
                        if not await handle.is_visible():
                            await handle.dispose()
                            continue
                        if ancestor is not None:
                            contained = await handle.evaluate(
                                "(element, root) => element === root || root.contains(element)",
                                ancestor.handle,
                            )
                            if not contained:
                                await handle.dispose()
                                continue
                        box = await handle.bounding_box()
                        if scope == "viewport" and box is not None:
                            if (
                                box["x"] + box["width"] < 0
                                or box["y"] + box["height"] < 0
                                or box["x"] > float(viewport.get("width") or 0)
                                or box["y"] > float(viewport.get("height") or 0)
                            ):
                                await handle.dispose()
                                continue
                        meta = await handle.evaluate(_ELEMENT_META_JS)
                        dom_id = str(meta.pop("dom_id"))
                        dom_key = f"{frame_id}:{dom_id}"
                        live_dom_keys.add(dom_key)
                        ref_name = state.refs_by_dom_key.get(dom_key)
                        if not ref_name:
                            state.ref_sequence += 1
                            ref_name = f"e{state.ref_sequence}"
                            state.refs_by_dom_key[dom_key] = ref_name
                        old = state.refs.get(ref_name)
                        if old is not None and old.handle != handle:
                            with contextlib.suppress(Exception):
                                await old.handle.dispose()
                        record = RefState(
                            ref=ref_name,
                            generation=state.generation,
                            handle=handle,
                            role=str(meta.get("role") or "generic"),
                            accessible_name=str(meta.get("accessible_name") or ""),
                            text=str(meta.get("text") or ""),
                            attributes={str(k): str(v) for k, v in dict(meta.get("attributes") or {}).items()},
                            allowed_actions=tuple(str(item) for item in meta.get("allowed_actions") or []),
                            frame_id=frame_id,
                            dom_key=dom_key,
                        )
                        state.refs[ref_name] = record
                        records.append(record)
                        if len(records) >= MAX_SNAPSHOT_REFS:
                            return records
                    except Exception:
                        with contextlib.suppress(Exception):
                            await handle.dispose()
        for ref_name, record in list(state.refs.items()):
            if record.generation == state.generation and record.dom_key not in live_dom_keys:
                with contextlib.suppress(Exception):
                    if not await record.handle.evaluate("element => element.isConnected"):
                        state.refs.pop(ref_name, None)
                        state.refs_by_dom_key.pop(record.dom_key, None)
        return records

    async def _rendered_text(
        self, page: Page, *, scope: str, max_chars: int
    ) -> tuple[str, bool]:
        sections: list[str] = []
        for index, frame in enumerate(page.frames):
            try:
                if scope == "viewport":
                    text = await frame.evaluate(_VIEWPORT_TEXT_JS)
                else:
                    text = await frame.locator("body").inner_text(timeout=3_000)
            except Exception:
                continue
            normalized = "\n".join(line.strip() for line in str(text).splitlines() if line.strip())
            if normalized:
                label = "main" if frame == page.main_frame else f"frame-{index}"
                sections.append(f"[{label}]\n{normalized}")
        output = "\n\n".join(sections)
        return output[:max_chars], len(output) > max_chars

    async def _change_summary(self, state: PageState) -> dict[str, Any]:
        previous = state.snapshot_lines.get("interactive")
        if previous is None:
            return {"added": [], "removed": [], "changed": False}
        async with state.lock:
            current_records = await self._collect_refs(
                state,
                scope="interactive",
                ancestor=None,
            )
            current = [self._format_ref(item) for item in current_records]
            added = [line for line in current if line not in previous]
            removed = [line for line in previous if line not in current]
            state.snapshot_lines["interactive"] = current
        return {
            "added": added[:20],
            "removed": removed[:20],
            "added_count": len(added),
            "removed_count": len(removed),
            "changed": bool(added or removed),
        }

    async def _page_response(
        self,
        state: PageState,
        message: str,
        *,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = await self._page_data(state)
        if extra:
            data.update(extra)
        return self._ok(message, data)

    async def _page_data(self, state: PageState) -> dict[str, Any]:
        title = ""
        with contextlib.suppress(Exception):
            title = await state.page.title()
        return {
            "page_id": state.page_id,
            "url": state.page.url,
            "title": title,
            "document_generation": state.generation,
            "network_cursor": state.network_cursor,
            "console_cursor": state.console_cursor,
            "download_cursor": state.download_cursor,
        }

    async def _target_id(self, page: Page) -> str:
        session = await page.context.new_cdp_session(page)
        try:
            payload = await session.send("Target.getTargetInfo")
            return str((payload.get("targetInfo") or {}).get("targetId") or "")
        finally:
            with contextlib.suppress(Exception):
                await session.detach()

    async def _minimize_all_windows(self) -> None:
        if os.environ.get("CHROME_WINDOW", "minimized") == "normal" or self._browser is None:
            return
        for context in self._browser.contexts:
            for page in context.pages:
                with contextlib.suppress(Exception):
                    await self._minimize_window(page)

    async def _window_state(self, page: Page) -> str:
        """Return the CDP windowState ('minimized'/'normal'/...) for ``page``."""
        session = await page.context.new_cdp_session(page)
        try:
            info = await session.send("Browser.getWindowForTarget")
            window_id = info.get("windowId")
            if window_id is None:
                return "unknown"
            bounds = await session.send(
                "Browser.getWindowBounds", {"windowId": window_id}
            )
            return str((bounds.get("bounds") or {}).get("windowState") or "unknown")
        finally:
            with contextlib.suppress(Exception):
                await session.detach()

    async def _all_windows_minimized(self) -> bool:
        """True when every live window is minimized (or no window is open).

        Used to decide whether a new page opened by the AI may steal focus: if
        the user has already restored a window (state != minimized), AXIOM must
        not force it back, per the browser-control SKILL contract.
        """
        if self._browser is None:
            return True
        for context in self._browser.contexts:
            for page in context.pages:
                with contextlib.suppress(Exception):
                    if await self._window_state(page) != "minimized":
                        return False
        return True

    async def _minimize_only_if_already_minimized(self) -> None:
        """Minimize all windows only when none has been restored by the user.

        Called on a fresh CDP attach so a window the operator opened manually
        (e.g. to log in and persist cookies) is never collapsed by AXIOM.
        """
        if os.environ.get("CHROME_WINDOW", "minimized") == "normal" or self._browser is None:
            return
        if not await self._all_windows_minimized():
            return
        for context in self._browser.contexts:
            for page in context.pages:
                with contextlib.suppress(Exception):
                    await self._minimize_window(page)

    async def _minimize_window(self, page: Page) -> None:
        session = await page.context.new_cdp_session(page)
        try:
            info = await session.send("Browser.getWindowForTarget")
            window_id = info.get("windowId")
            if window_id is not None:
                await session.send(
                    "Browser.setWindowBounds",
                    {"windowId": window_id, "bounds": {"windowState": "minimized"}},
                )
        finally:
            with contextlib.suppress(Exception):
                await session.detach()

    def _frame_id(self, state: PageState, frame: Frame) -> str:
        key = id(frame)
        existing = state.frame_ids.get(key)
        if existing:
            return existing
        if frame == state.page.main_frame:
            value = "main"
        else:
            state.frame_sequence += 1
            value = f"f{state.frame_sequence}"
        state.frame_ids[key] = value
        return value

    def _page(self, lease: LeaseState, params: dict[str, Any]) -> PageState:
        page_id = str(params.get("page_id") or "")
        if not page_id:
            raise BrowserBrokerError("page_id_required", "该浏览器工具必须提供page_id")
        state = lease.pages.get(page_id)
        if state is None or state.page.is_closed():
            raise BrowserBrokerError("page_not_found", f"当前Browser Lease没有页面 {page_id}")
        return state

    def _ref(self, state: PageState, value: str, *, action: str) -> RefState:
        if not value:
            raise BrowserBrokerError("ref_required", "该浏览器操作必须提供ref")
        ref = state.refs.get(value)
        if ref is None or ref.generation != state.generation:
            raise BrowserBrokerError("stale_ref", f"Ref {value} 已失效，请重新执行browser_snapshot")
        if action not in ref.allowed_actions:
            raise BrowserBrokerError(
                "action_not_allowed",
                f"Ref {value} 不允许 {action}；允许操作：{','.join(ref.allowed_actions)}",
            )
        return ref

    async def _require_live_ref(self, ref: RefState) -> None:
        try:
            connected = await ref.handle.evaluate("element => element.isConnected")
        except Exception as exc:
            raise BrowserBrokerError("stale_ref", f"Ref {ref.ref} 已失效，请重新Snapshot") from exc
        if not connected:
            raise BrowserBrokerError("stale_ref", f"Ref {ref.ref} 已失效，请重新Snapshot")

    async def _clear_refs(self, state: PageState) -> None:
        for ref in list(state.refs.values()):
            with contextlib.suppress(Exception):
                await ref.handle.dispose()
        state.refs.clear()
        state.refs_by_dom_key.clear()

    def _require_target(self, lease: LeaseState, url: str) -> None:
        if not target_allowed(lease.scope, url):
            raise BrowserBrokerError("target_out_of_scope", f"目标不在当前Task Scope：{url}")

    def _inside_run_dir(self, lease: LeaseState, value: str) -> Path:
        candidate = Path(value)
        resolved = (
            candidate.resolve()
            if candidate.is_absolute()
            else (lease.run_dir / candidate).resolve()
        )
        if resolved != lease.run_dir and lease.run_dir not in resolved.parents:
            raise BrowserBrokerError("path_outside_run", "文件路径必须位于当前运行目录")
        return resolved

    @staticmethod
    def _timeout_ms(params: dict[str, Any]) -> int:
        try:
            value = int(params.get("timeout_ms") or DEFAULT_ACTION_TIMEOUT_MS)
        except (TypeError, ValueError):
            value = DEFAULT_ACTION_TIMEOUT_MS
        return min(max(value, 250), MAX_ACTION_TIMEOUT_MS)

    @staticmethod
    def _public_ref(ref: RefState) -> dict[str, Any]:
        return {
            "ref": ref.ref,
            "role": ref.role,
            "accessible_name": ref.accessible_name,
            "text": ref.text,
            "attributes": ref.attributes,
            "allowed_actions": list(ref.allowed_actions),
            "frame": ref.frame_id,
        }

    def _format_ref(self, ref: RefState) -> str:
        name = ref.accessible_name or ref.text
        attributes = " ".join(f"{key}={value!r}" for key, value in ref.attributes.items())
        suffix = f" {attributes}" if attributes else ""
        return (
            f"[{ref.role} ref={ref.ref} actions={','.join(ref.allowed_actions)} frame={ref.frame_id}] "
            f"{name}{suffix}"
        ).strip()

    @staticmethod
    def _network_public(entry: dict[str, Any], *, include_sensitive: bool) -> dict[str, Any]:
        result = dict(entry)
        headers = result.get("headers")
        if isinstance(headers, dict):
            result["headers"] = redact_headers(headers, include_sensitive=include_sensitive)
        for key in ("body", "post_data", "payload"):
            if isinstance(result.get(key), str):
                value = str(result[key])
                result[key] = value[:16_000]
                if len(value) > 16_000:
                    result[f"{key}_truncated"] = True
        return result

    @staticmethod
    def _ok(content: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"ok": True, "content": content[:MAX_RESPONSE_CHARS], "data": data or {}}

    @staticmethod
    def _error(code: str, message: str, *, retryable: bool = False) -> dict[str, Any]:
        return {
            "ok": False,
            "content": f"错误：{message}",
            "error": {"code": code, "message": message, "retryable": retryable},
        }


_ELEMENT_META_JS = r"""
(element => {
  const root = globalThis;
  if (!root.__axiomRefIds) {
    root.__axiomRefIds = new WeakMap();
    root.__axiomRefCounter = 0;
  }
  let domId = root.__axiomRefIds.get(element);
  if (!domId) {
    domId = String(++root.__axiomRefCounter);
    root.__axiomRefIds.set(element, domId);
  }
  const tag = element.tagName.toLowerCase();
  const type = (element.getAttribute('type') || '').toLowerCase();
  const explicitRole = element.getAttribute('role') || '';
  const roles = {
    a: 'link', button: 'button', textarea: 'textbox', select: 'combobox',
    summary: 'button', option: 'option'
  };
  let role = explicitRole || roles[tag] || 'generic';
  if (tag === 'input') {
    role = ({checkbox:'checkbox', radio:'radio', button:'button', submit:'button', reset:'button', range:'slider'}[type] || 'textbox');
  }
  const labelledBy = (element.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean)
    .map(id => document.getElementById(id)?.innerText || '').join(' ');
  const labels = element.labels ? Array.from(element.labels).map(label => label.innerText || '').join(' ') : '';
  const text = (element.innerText || element.value || '').replace(/\s+/g, ' ').trim().slice(0, 160);
  const accessibleName = (
    element.getAttribute('aria-label') || labelledBy || labels || element.getAttribute('alt') ||
    element.getAttribute('title') || element.getAttribute('placeholder') || text
  ).replace(/\s+/g, ' ').trim().slice(0, 160);
  const attrs = {};
  for (const name of ['id','name','type','placeholder','href','aria-expanded','aria-checked','aria-selected']) {
    const value = element.getAttribute(name);
    if (value) attrs[name] = value.slice(0, 240);
  }
  if (element.disabled) attrs.disabled = 'true';
  if (element.checked) attrs.checked = 'true';
  const actions = new Set(['scroll']);
  if (!element.disabled) {
    actions.add('click');
    actions.add('hover');
    if (['button','a','summary','select'].includes(tag) || ['button','link','tab','menuitem','combobox'].includes(role)) actions.add('press');
    if (tag === 'input' || tag === 'textarea' || element.isContentEditable || role === 'textbox') {
      if (!['checkbox','radio','button','submit','reset','file'].includes(type)) actions.add('fill');
      actions.add('press');
    }
    if (tag === 'select' || role === 'combobox') actions.add('select');
    if (type === 'checkbox' || type === 'radio' || role === 'checkbox' || role === 'radio' || role === 'switch') actions.add('check');
    if (tag === 'input' && type === 'file') actions.add('upload');
  }
  return {dom_id: domId, role, accessible_name: accessibleName, text, attributes: attrs, allowed_actions: Array.from(actions)};
})
"""


_VIEWPORT_TEXT_JS = r"""
(() => {
  const visible = element => {
    const rect = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    return rect.width > 0 && rect.height > 0 && rect.bottom >= 0 && rect.right >= 0 &&
      rect.top <= innerHeight && rect.left <= innerWidth && style.visibility !== 'hidden' && style.display !== 'none';
  };
  const nodes = Array.from(document.querySelectorAll('h1,h2,h3,h4,p,li,td,th,label,a,button,input,textarea,select'));
  return nodes.filter(visible).map(element => (
    element.innerText || element.value || element.getAttribute('aria-label') || element.getAttribute('placeholder') || ''
  ).replace(/\s+/g, ' ').trim()).filter(Boolean).join('\n');
})()
"""
