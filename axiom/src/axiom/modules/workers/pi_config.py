from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PiWorkerConfig(BaseModel):
    """Canonical persisted configuration for a Pi worker.

    Runtime environment variables which are not first-class Pi settings stay
    in ``environment``.  Keeping one persisted shape avoids the historical
    ``config.env`` / top-level split that made a Worker look configured in the
    UI while the Runner could not start it.
    """

    model_config = ConfigDict(extra="allow")

    binary: str = "pi"
    provider: str = "axiom"
    model: str = ""
    base_url: str = ""
    provider_api: str = "openai-completions"
    api_key: str = ""
    thinking: str = "off"
    context_window: int = Field(default=200_000, ge=1)
    tools: list[str] = Field(default_factory=lambda: ["read", "write", "bash", "edit"])
    skill_paths: list[str] = Field(default_factory=list)
    session_dir: str = ""
    session_flag: str = "--session-id"
    environment: dict[str, str] = Field(default_factory=dict)


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

    if "context_window" in source:
        try:
            source["context_window"] = int(source["context_window"])
        except (TypeError, ValueError):
            source["context_window"] = 200_000

    source["environment"] = environment
    return PiWorkerConfig.model_validate(source).model_dump(mode="json", exclude_none=True)
