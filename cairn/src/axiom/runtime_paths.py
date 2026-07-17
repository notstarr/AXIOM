from __future__ import annotations

import os
from pathlib import Path


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name, "").strip()
    return Path(value).expanduser() if value else None


def runtime_dir() -> Path | None:
    return _env_path("AXIOM_RUNTIME_DIR")


def path_from_env(name: str, default: str | Path, runtime_child: str | None = None) -> Path:
    explicit = _env_path(name)
    if explicit is not None:
        return explicit
    root = runtime_dir()
    if root is not None and runtime_child:
        return root / runtime_child
    return Path(default).expanduser()


def db_path(default: Path) -> Path:
    explicit = _env_path("AXIOM_DB_PATH")
    if explicit is not None:
        return explicit
    root = runtime_dir()
    if root is not None:
        return root / "db.sqlite"
    return default


def files_root() -> Path:
    return path_from_env("AXIOM_FILES_ROOT", "/tmp/axiom-files", "files")


def project_ai_dir(parent_project_id: str | None, project_id: str) -> Path:
    return files_root() / (parent_project_id or "unknown") / project_id / "ai"


def uploads_root() -> Path:
    return path_from_env("AXIOM_UPLOAD_DIR", "/tmp/axiom-uploads", "uploads")


def pi_root() -> Path:
    return path_from_env("AXIOM_PI_ROOT", "/tmp/axiom-pi", "pi")


def prompts_root() -> Path:
    return path_from_env("AXIOM_PROMPTS_ROOT", "/tmp/axiom-prompts", "prompts")


def debug_root() -> Path:
    return path_from_env("AXIOM_DEBUG_DIR", "/tmp/axiom-debug", "debug")


def tools_root() -> Path:
    return path_from_env("AXIOM_TOOLS_ROOT", "~/.axiom/tools", "tools")


def crawler_root() -> Path:
    return path_from_env("AXIOM_CRAWLER_ROOT", "/tmp/axiom-crawler", "crawler")


def crawler_session_dir() -> Path:
    return path_from_env("AXIOM_CRAWLER_SESSION_DIR", "/tmp/axiom-crawler", "crawler/session")


def crawler_traffic_file() -> Path:
    return path_from_env("AXIOM_CRAWLER_TRAFFIC_FILE", "/tmp/proxy_flows.jsonl", "crawler/proxy_flows.jsonl")


def crawler_pid_path(project_id: str) -> Path:
    explicit_root = runtime_dir() or _env_path("AXIOM_CRAWLER_ROOT")
    if explicit_root is not None:
        return crawler_root() / "pids" / f"{project_id}.pid"
    return Path(f"/tmp/axiom-crawler-{project_id}.pid")


def crawler_meta_path(project_id: str) -> Path:
    explicit_root = runtime_dir() or _env_path("AXIOM_CRAWLER_ROOT")
    if explicit_root is not None:
        return crawler_root() / "meta" / f"{project_id}.meta.json"
    return Path(f"/tmp/axiom-crawler-{project_id}.meta.json")


def crawler_log_path(project_id: str) -> Path:
    explicit_root = runtime_dir() or _env_path("AXIOM_CRAWLER_ROOT")
    if explicit_root is not None:
        return crawler_root() / "logs" / f"{project_id}.log"
    return Path(f"/tmp/axiom-crawler-{project_id}.log")
