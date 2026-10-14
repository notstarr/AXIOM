from __future__ import annotations

import json
import os
import re
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator


PI_RUNTIME_VERSION = "1.1.0"
PI_NODE_VERSION = "v22.22.2"
PI_RUNTIME_BINARY = (
    Path(__file__).resolve().parents[4] / "runtime" / "pi" / PI_RUNTIME_VERSION / "bin" / "pi"
)
# The workbuddy-managed Node install uses package-revision directory suffixes
# (22.22.2, 22.22.2-2, ...) for the same pinned Node version.  Resolve the
# newest matching revision at import time instead of pinning one path that
# silently disappears on a workbuddy runtime update.
_AXIOM_PI_NODE_ENV = "AXIOM_PI_NODE_BINARY"
_PI_NODE_VERSIONS_DIR = Path.home() / ".workbuddy" / "binaries" / "node" / "versions"


def _resolve_pi_node_binary() -> Path:
    override = os.environ.get(_AXIOM_PI_NODE_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    exact = _PI_NODE_VERSIONS_DIR / PI_NODE_VERSION.lstrip("v") / "bin" / "node"
    if exact.is_file():
        return exact
    candidates = sorted(
        (
            item / "bin" / "node"
            for item in _PI_NODE_VERSIONS_DIR.glob(f"{PI_NODE_VERSION.lstrip('v')}*")
            if (item / "bin" / "node").is_file()
        ),
        key=lambda path: path.stat().st_mtime,
    )
    if candidates:
        return candidates[-1]
    # 仓库 vendored 运行时兜底（容器部署场景：linux x86_64 构建随镜像分发）
    vendored = (
        Path(__file__).resolve().parents[4]
        / "runtime" / "node" / PI_NODE_VERSION.lstrip("v") / "bin" / "node"
    )
    if vendored.is_file():
        return vendored
    return exact


PI_NODE_BINARY = _resolve_pi_node_binary()
_LEGACY_PI_BINARIES = frozenset({"", "pi", "/usr/local/bin/pi"})
_PI_PACKAGE_RELATIVE_PATH = Path(
    "node_modules", "@earendil-works", "pi-coding-agent", "package.json"
)
_PI_PACKAGE_NAME = "@earendil-works/pi-coding-agent"
PiThinkingLevel = Literal["minimal", "low", "medium", "high", "xhigh", "max"]
_PI_THINKING_LEVELS = frozenset({"minimal", "low", "medium", "high", "xhigh", "max"})
ANTHROPIC_API_VERSION = "2023-06-01"


def canonical_provider_api(provider_api: str | None) -> str:
    """Return the Pi API identifier used by AXIOM's standard providers.

    ``responses`` was accepted by older configurations.  Keep it readable
    while using Pi's current provider identifier at request boundaries.
    Unknown values remain untouched for custom Pi provider extensions.
    """

    value = str(provider_api or "").strip()
    if not value:
        return "openai-completions"
    if value == "responses":
        return "openai-responses"
    # Pi 0.85 exposed Azure's Responses implementation under this internal
    # API name.  Pi 1.1 owns the implementation and expects the provider API
    # identifier ``azure``; keep old Worker rows readable without changing
    # their model, key, or endpoint values.
    if value == "azure-openai-responses":
        return "azure"
    return value


def is_anthropic_messages(provider_api: str | None) -> bool:
    return canonical_provider_api(provider_api) == "anthropic-messages"


def _root_base_url(base_url: str | None) -> str:
    """Normalize any worker base_url spelling to the provider root."""

    root = (base_url or "").strip().rstrip("/")
    if root.lower().endswith("/v1"):
        return root[: -len("/v1")]
    return root


def sdk_base_url(provider_api: str | None, base_url: str) -> str:
    """Base URL handed to the worker-side SDK (pi ``models.json`` / env).

    AXIOM persists the provider root (``https://host``) for every provider
    API.  The Anthropic SDK appends ``/v1/messages`` itself, so it must
    receive the root unchanged; OpenAI-family SDKs post bare resource paths
    (``/chat/completions`` / ``/responses``) and therefore expect ``/v1``
    inside the baseURL — matching the official ``api.openai.com/v1`` form.
    """

    root = _root_base_url(base_url)
    if not root or is_anthropic_messages(provider_api):
        return root
    if canonical_provider_api(provider_api) == "azure":
        if root.endswith("/openai"):
            return f"{root}/v1"
        if root.endswith("/openai/v1"):
            return root
        return f"{root}/openai/v1"
    return root + "/v1"


def upstream_request_url(provider_api: str | None, base_url: str, path: str) -> str:
    """Full upstream URL for a worker-relative request path.

    Every provider upstream serves under ``/v1/``.  Pi-bound SDK paths carry
    the ``v1/`` prefix for anthropic (SDK-baked ``/v1/messages``) but not for
    openai (bare ``/chat/completions``); normalize to exactly one prefix.
    """

    root = _root_base_url(base_url)
    p = (path or "").strip().lstrip("/")
    if canonical_provider_api(provider_api) == "azure" and not root.endswith("/openai"):
        root = f"{root}/openai"
    if p and not p.startswith("v1/"):
        p = "v1/" + p
    return f"{root}/{p}" if p else root


def provider_auth_headers(provider_api: str | None, api_key: str) -> dict[str, str]:
    """Build the provider-specific authentication headers for a request."""

    if is_anthropic_messages(provider_api):
        headers = {"anthropic-version": ANTHROPIC_API_VERSION}
        if api_key:
            headers["x-api-key"] = api_key
        return headers
    if canonical_provider_api(provider_api) == "azure":
        return {"api-key": api_key} if api_key else {}
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


DEVELOPER_ROLE_PROBE_TIMEOUT_SECONDS = 15.0


def _probe_role_accepted(
    base_url: str,
    api_key: str,
    model: str,
    provider_api: str,
    role: str,
    *,
    proxy: str | None = None,
    timeout: float = DEVELOPER_ROLE_PROBE_TIMEOUT_SECONDS,
) -> bool | None:
    """Ask the gateway to accept one system-level ``role``.

    Returns ``True`` when the request is accepted, ``False`` when the gateway
    refuses it, and ``None`` when the outcome says nothing about the role
    (transport failure or an unhelpful status).  Callers must treat ``None``
    as "unknown" rather than "unsupported".
    """

    payload = {
        "model": model,
        "messages": [
            {"role": role, "content": "ping"},
            {"role": "user", "content": "ping"},
        ],
        "max_tokens": 1,
    }
    headers = {
        "Content-Type": "application/json",
        **provider_auth_headers(provider_api, api_key),
    }
    try:
        response = httpx.post(
            upstream_request_url(provider_api, base_url, "chat/completions"),
            headers=headers,
            json=payload,
            timeout=timeout,
            proxy=proxy,
        )
    except httpx.HTTPError:
        return None
    if 200 <= response.status_code < 300:
        return True
    return False


def probe_developer_role(
    base_url: str,
    api_key: str,
    model: str,
    provider_api: str,
    *,
    proxy: str | None = None,
    timeout: float = DEVELOPER_ROLE_PROBE_TIMEOUT_SECONDS,
) -> bool | None:
    """Decide whether a gateway accepts OpenAI's ``developer`` message role.

    Pi sends the system prompt as ``role: "developer"`` whenever the model
    advertises reasoning, which many OpenAI-compatible gateways still reject.
    A single rejection is not evidence: the same status surfaces for quota,
    routing and model-name problems.  So probe twice with the role as the only
    variable and only claim unsupported when ``developer`` fails while
    ``system`` succeeds.

    ``True``  - the gateway accepts ``developer``; Pi may keep using it.
    ``False`` - ``developer`` refused but ``system`` accepted: use ``system``.
    ``None``  - inconclusive (both refused, or transport failure); change nothing.
    """

    if not base_url or not model:
        return None
    api = canonical_provider_api(provider_api)
    # Only the OpenAI chat-completions shape carries the developer role here;
    # Anthropic has no such role and the responses API needs a different probe.
    if api != "openai-completions":
        return None

    developer = _probe_role_accepted(
        base_url,
        api_key,
        model,
        api,
        "developer",
        proxy=proxy,
        timeout=timeout,
    )
    if developer:
        return True
    if developer is None:
        return None

    system = _probe_role_accepted(
        base_url,
        api_key,
        model,
        api,
        "system",
        proxy=proxy,
        timeout=timeout,
    )
    if system:
        return False
    return None


class PiRuntimeError(RuntimeError):
    """The configured Pi runtime does not match AXIOM's supported runtime."""


class PiWorkerConfig(BaseModel):
    """Canonical persisted configuration for a Pi worker.

    Runtime environment variables which are not first-class Pi settings stay
    in ``environment``.  Keeping one persisted shape avoids the historical
    ``config.env`` / top-level split that made a Worker look configured in the
    UI while the Runner could not start it.
    """

    model_config = ConfigDict(extra="allow")

    binary: str = str(PI_RUNTIME_BINARY)
    provider: str = "axiom"
    model: str = ""
    base_url: str = ""
    provider_api: str = "openai-completions"
    api_key: str = ""
    context_window: int = Field(default=200_000, ge=1)
    thinking_enabled: bool = False
    thinking_level: PiThinkingLevel = "medium"
    # 该后端的 LLM 请求是否经 serve 网关代理（网关跳挂全局代理）。
    # 默认 False：直连 provider，与旧"全局关代理"路径完全一致。
    llm_proxy: bool = False
    # 网关是否接受 OpenAI 的 ``developer`` 消息角色（``system`` 的新名字）。
    # ``None`` 未知：不下发 compat，Pi 按默认走 developer；
    # ``False`` 已确证不支持：下发 ``compat.supportsDeveloperRole=false``，Pi 改用 system；
    # ``True``  已确证支持：同样不下发 compat，行为与未知一致但不再重复探测。
    developer_role_supported: bool | None = None
    tools: list[str] = Field(default_factory=lambda: ["read", "write", "bash", "edit"])
    skill_paths: list[str] = Field(default_factory=list)
    session_dir: str = ""
    session_flag: str = "--session-id"
    environment: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_thinking(cls, value: Any) -> Any:
        """Split the historical ``thinking=off|level`` setting safely.

        Pi treats model reasoning capability and the selected thinking level as
        separate inputs.  AXIOM used to persist both concepts in one string,
        and its generated ``models.json`` did not advertise reasoning at all,
        so Pi clamped every requested non-off level back to ``off``.  Keep old
        Workers readable while making the two real controls first-class.
        """

        if not isinstance(value, dict):
            return value
        source = dict(value)
        base_url = str(source.get("base_url", "") or "").strip().rstrip("/")
        if base_url.lower().endswith("/v1"):
            # Persist the provider root; /v1 is derived per provider API at
            # the SDK/gateway boundary (sdk_base_url / upstream_request_url).
            base_url = base_url[: -len("/v1")]
        source["base_url"] = base_url
        legacy = str(source.pop("thinking", "") or "").strip().lower()
        source.pop("thinking_capable", None)
        source.pop("thinking_tested_at", None)

        raw_level = str(source.get("thinking_level", "") or "").strip().lower()
        if raw_level == "off":
            source["thinking_enabled"] = False
            source["thinking_level"] = "medium"
        elif raw_level in _PI_THINKING_LEVELS:
            source["thinking_level"] = raw_level
        elif not raw_level and legacy in _PI_THINKING_LEVELS:
            source["thinking_level"] = legacy

        if "thinking_enabled" not in source and legacy:
            source["thinking_enabled"] = legacy != "off"
        return source

    @property
    def effective_thinking_level(self) -> str:
        """Return the exact Pi CLI thinking level for this Worker."""

        return self.thinking_level if self.thinking_enabled else "off"


_LEGACY_ENV_FIELDS = {
    "PI_BINARY_PATH": "binary",
    "PI_MODEL": "model",
    "PI_BASE_URL": "base_url",
    "PI_PROVIDER_API": "provider_api",
    "PI_API_KEY": "api_key",
    "PI_THINKING": "thinking",
    "PI_MODEL_CONTEXT_WINDOW": "context_window",
}


def canonicalize_pi_worker_config(config: dict[str, Any] | None) -> dict[str, Any]:
    """Convert every supported historical Pi config shape to one shape.

    The function is intentionally tolerant so imported/legacy Workers can be
    opened and saved before every required runtime value has been entered.
    The Runner performs the strict executable check when it is instantiated.
    """

    source = dict(config or {})
    legacy_env = source.pop("env", {})
    if not isinstance(legacy_env, dict):
        legacy_env = {}

    environment = source.get("environment", {})
    if not isinstance(environment, dict):
        environment = {}
    environment = {str(key): str(value) for key, value in environment.items()}

    for key, value in legacy_env.items():
        field = _LEGACY_ENV_FIELDS.get(str(key))
        if field:
            if field not in source or source[field] in (None, ""):
                source[field] = value
        else:
            environment[str(key)] = str(value)

    binary = str(source.get("binary") or "").strip()
    if binary in _LEGACY_PI_BINARIES:
        source["binary"] = str(PI_RUNTIME_BINARY)
    else:
        # 版本化运行时路径自动迁移：worker 配置在创建时固化了当时的
        # runtime/pi/<version>/bin/pi 绝对路径；底座升级后旧路径必然失效。
        # 指向旧版本化运行时的一律重写到当前钉定版本（自定义路径不受影响）。
        match = re.search(r"[\\/]runtime[\\/]pi[\\/][^\\/]+[\\/]bin[\\/]pi$", binary)
        if match and Path(binary).resolve() != PI_RUNTIME_BINARY.resolve():
            source["binary"] = str(PI_RUNTIME_BINARY)

    if "context_window" in source:
        try:
            source["context_window"] = int(source["context_window"])
        except (TypeError, ValueError):
            source["context_window"] = 200_000

    if str(source.get("provider_api") or "").strip() == "azure-openai-responses":
        source["provider_api"] = "azure"

    source["environment"] = environment
    return PiWorkerConfig.model_validate(source).model_dump(mode="json", exclude_none=True)


def validate_pi_worker_runtime(config: dict[str, Any] | None) -> PiWorkerConfig:
    """Normalize a Worker config and verify the pinned Pi/Node runtime once."""

    settings = PiWorkerConfig.model_validate(canonicalize_pi_worker_config(config))
    _validate_pi_runtime(settings.binary)
    return settings


@lru_cache(maxsize=4)
def _validate_pi_runtime(binary: str) -> None:
    configured = Path(binary).expanduser()
    expected = PI_RUNTIME_BINARY
    try:
        configured_resolved = configured.resolve()
        expected_resolved = expected.resolve()
    except OSError as exc:
        raise PiRuntimeError(f"无法解析 Pi 运行时路径：{binary}") from exc
    if configured_resolved != expected_resolved:
        raise PiRuntimeError(
            "Pi Worker 必须使用版本化运行时："
            f"{expected}；当前配置为：{binary}"
        )
    _require_pi_runtime_version(expected, PI_RUNTIME_VERSION)
    _require_runtime_version(PI_NODE_BINARY, PI_NODE_VERSION, label="Node")


def _require_pi_runtime_version(binary: Path, expected: str) -> None:
    """Validate Pi without starting its full CLI/bootstrap path.

    ``pi --version`` is not a cheap metadata lookup in Pi 0.82.x: it can load
    extensions and perform startup/catalog work.  Running it during every
    Runner claim made a healthy Bootstrap lease look like
    ``runner_dispatch_failed`` when the five-second probe timed out.  The
    pinned package metadata is local and authoritative for the version check.
    """

    _require_executable(binary, label="Pi")
    metadata = binary.expanduser().parent.parent / _PI_PACKAGE_RELATIVE_PATH
    if not metadata.is_file():
        raise PiRuntimeError(f"Pi 运行时元数据不存在：{metadata}")
    try:
        payload = json.loads(metadata.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PiRuntimeError(f"无法读取 Pi 运行时元数据：{metadata}") from exc
    if not isinstance(payload, dict):
        raise PiRuntimeError(f"Pi 运行时元数据格式错误：{metadata}")

    package_name = str(payload.get("name") or "").strip()
    if package_name != _PI_PACKAGE_NAME:
        raise PiRuntimeError(
            f"Pi 运行时包错误：期望 {_PI_PACKAGE_NAME}，实际 {package_name or '未知'}"
        )
    actual_version = str(payload.get("version") or "").strip()
    if actual_version != expected:
        raise PiRuntimeError(
            f"Pi 运行时版本错误：期望 {expected}，实际 {actual_version or '未知'}"
        )

    package_root = metadata.parent.resolve()
    bin_field = payload.get("bin")
    cli_relative = bin_field.get("pi") if isinstance(bin_field, dict) else ""
    if not isinstance(cli_relative, str) or not cli_relative.strip():
        raise PiRuntimeError(f"Pi 运行时元数据缺少 bin.pi：{metadata}")
    try:
        cli_entry = (package_root / cli_relative).resolve()
        cli_entry.relative_to(package_root)
    except (OSError, ValueError) as exc:
        raise PiRuntimeError(f"Pi CLI 入口越界或无法解析：{cli_relative}") from exc
    _require_executable(cli_entry, label="Pi CLI")


def _require_executable(binary: Path, *, label: str) -> None:
    if not binary.is_file():
        raise PiRuntimeError(f"{label} 运行时不存在：{binary}")
    if not binary.stat().st_mode & 0o111:
        raise PiRuntimeError(f"{label} 运行时不可执行：{binary}")


def _require_runtime_version(binary: Path, expected: str, *, label: str) -> None:
    _require_executable(binary, label=label)
    try:
        result = subprocess.run(
            [str(binary), "--version"],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PiRuntimeError(f"无法验证 {label} 运行时：{binary}") from exc
    actual = (result.stdout.strip() or result.stderr.strip()).splitlines()[0:1]
    actual_version = actual[0].strip() if actual else ""
    if result.returncode != 0 or actual_version != expected:
        raise PiRuntimeError(
            f"{label} 运行时版本错误：期望 {expected}，实际 {actual_version or '未知'}"
        )


def llm_proxy_for(config: Any, backend: PiWorkerConfig) -> str | None:
    """该后端 LLM 出流的代理 URL；None = 直连。

    开关（backend.llm_proxy）表达意图，全局配置表达可用性：
    开且全局 mode==http 且 URL 非空 → 代理 URL；其余一律 None（直连）。
    三个消费点（llm-gateway 转发、测试连接、获取模型）必须统一走本函数。
    """
    if not backend.llm_proxy:
        return None
    if getattr(config, "network_proxy_mode", "off") == "http":
        url = (getattr(config, "network_proxy_url", "") or "").strip()
        if url:
            return url
    return None
