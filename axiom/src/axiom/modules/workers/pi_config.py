from __future__ import annotations

import json
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


PI_RUNTIME_VERSION = "0.82.1"
PI_NODE_VERSION = "v22.22.2"
PI_RUNTIME_BINARY = (
    Path(__file__).resolve().parents[4] / "runtime" / "pi" / PI_RUNTIME_VERSION / "bin" / "pi"
)
PI_NODE_BINARY = Path(
    "/Users/hexin/.workbuddy/binaries/node/versions/22.22.2/bin/node"
)
_LEGACY_PI_BINARIES = frozenset({"", "pi", "/usr/local/bin/pi"})
_PI_PACKAGE_RELATIVE_PATH = Path(
    "node_modules", "@earendil-works", "pi-coding-agent", "package.json"
)
_PI_PACKAGE_NAME = "@earendil-works/pi-coding-agent"
PiThinkingLevel = Literal["minimal", "low", "medium", "high", "xhigh", "max"]
_PI_THINKING_LEVELS = frozenset({"minimal", "low", "medium", "high", "xhigh", "max"})


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
        """Return the exact Pi 0.82.1 CLI level for this Worker."""

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

    if "context_window" in source:
        try:
            source["context_window"] = int(source["context_window"])
        except (TypeError, ValueError):
            source["context_window"] = 200_000

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
