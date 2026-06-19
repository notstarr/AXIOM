"""crawler/supervisor.py — the phased crawler supervisor loop and phase execution."""

from __future__ import annotations

import json
import os
import pathlib
import signal
import subprocess
import time
from typing import Any

from axiom.shared.output_parser import extract_json_object
from axiom.server.db import get_conn
from axiom.server.services import utcnow

from .constants import (
    CRAWLER_EXECUTE_PROMPT_PATH,
    CRAWLER_FLUSH_PROMPT_PATH,
    CRAWLER_SESSION_DIR,
    CRAWL_TOOLS,
    FLUSH_TOOLS,
)
from .meta import read_meta, write_meta, load_worker_config, load_crawler_runtime_settings
from .state import apply_crawler_increment
from .traffic import merge_recent_traffic_into_state, fallback_persist_recent_traffic
from .cdp import ensure_crawler_tab, close_crawler_tab
from .prompt import build_prompt


# ── Payload extraction ─────────────────────

def _unwrap_accepted_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    accepted = payload.get("accepted")
    if accepted is False:
        return None
    if accepted is True:
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ValueError("crawler data must be an object")
        return data
    return payload if isinstance(payload, dict) else None


def _validate_crawler_increment_payload(payload: dict[str, Any]) -> dict[str, Any]:
    data = _unwrap_accepted_payload(payload)
    if data is None:
        raise RuntimeError("crawler payload rejected")
    increment = data.get("increment", data)
    if not isinstance(increment, dict):
        raise ValueError("crawler increment must be an object")
    normalized = {
        "description": str(increment.get("description") or data.get("description") or "").strip(),
        "pages": increment.get("pages") or [],
        "apis": increment.get("apis") or [],
        "mappings": increment.get("mappings") or [],
    }
    for key in ("pages", "apis", "mappings"):
        if not isinstance(normalized[key], list):
            raise ValueError(f"{key} must be an array")
        for index, item in enumerate(normalized[key]):
            if not isinstance(item, dict):
                raise ValueError(f"{key}[{index}] must be an object")
            if not item.get("id"):
                raise ValueError(f"{key}[{index}].id is required")
    if not any(normalized[key] for key in ("pages", "apis", "mappings")):
        raise ValueError("crawler increment cannot be empty")
    return normalized


def _extract_phase_increment_payload(session_dir: str, *, log_path: str | None = None, log_offset: int = 0) -> dict[str, Any] | None:
    def _try_candidates(text: str) -> dict[str, Any] | None:
        for candidate in reversed([line.strip() for line in text.splitlines() if line.strip().startswith("{") and line.strip().endswith("}")]):
            try:
                payload = json.loads(candidate)
                return _validate_crawler_increment_payload(payload)
            except Exception:
                continue
        try:
            payload = extract_json_object(text)
            return _validate_crawler_increment_payload(payload)
        except Exception:
            return None

    directory = pathlib.Path(session_dir)
    if directory.exists():
        files = sorted(
            [path for path in directory.iterdir() if path.is_file() and path.suffix == ".jsonl"],
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for path in files:
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            assistant_texts: list[str] = []
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if message.get("type") != "message":
                    continue
                body = message.get("message") or {}
                if body.get("role") != "assistant":
                    continue
                for part in body.get("content") or []:
                    if isinstance(part, dict) and part.get("type") == "text":
                        assistant_texts.append(str(part.get("text") or ""))
            for candidate in reversed(assistant_texts):
                payload = _try_candidates(candidate)
                if payload is not None:
                    return payload
    if log_path:
        try:
            with pathlib.Path(log_path).open("r", encoding="utf-8") as fh:
                if log_offset > 0:
                    fh.seek(log_offset)
                text = fh.read()
        except OSError:
            return None
        return _try_candidates(text)
    return None


# ── Misc helpers ───────────────────────────

def _log_contains_success_summary(log_path: str, *, offset: int = 0) -> bool:
    path = pathlib.Path(log_path)
    if not path.exists():
        return False
    try:
        with path.open("r", encoding="utf-8") as fh:
            if offset > 0:
                fh.seek(offset)
            text = fh.read()
    except OSError:
        return False
    return '"accepted": true' in text and "description" in text


def _build_models_json(cfg: dict) -> str:
    model: dict = {"id": cfg["pi_model"], "name": cfg["pi_model"]}
    ctx = cfg.get("pi_context_window", "")
    if ctx:
        try:
            model["contextWindow"] = int(ctx)
        except ValueError:
            pass
    provider = {
        "baseUrl": cfg["pi_base_url"],
        "api": cfg.get("pi_provider_api", "openai-completions"),
        "apiKey": cfg.get("pi_api_key", ""),
        "models": [model],
    }
    return json.dumps({"providers": {"axiom": provider}}, ensure_ascii=False, separators=(",", ":"))


def _build_pi_argv(*, cfg: dict, agent_dir: str, session_dir: str, prompt: str, tools: str) -> list[str]:
    models_json = _build_models_json(cfg)
    wrap_script = (
        'agent_dir="$1"\n'
        'models_json="$2"\n'
        "shift 2\n"
        'mkdir -p "$agent_dir"\n'
        'mkdir -p "$agent_dir/sessions"\n'
        'printf "%s" "$models_json" > "$agent_dir/models.json"\n'
        'exec env PI_CODING_AGENT_DIR="$agent_dir" pi "$@"\n'
    )
    pi_args = [
        "--no-extensions",
        "--no-prompt-templates",
        "--no-themes",
        "--no-context-files",
        "--tools", tools,
        "--provider", "axiom",
        "--model", cfg["pi_model"],
        "--session-dir", session_dir,
        "-p", prompt,
    ]
    return ["/bin/sh", "-lc", wrap_script, "--", agent_dir, models_json, *pi_args]


# ── Phase execution ────────────────────────

def run_phase_once(*, cfg: dict, meta: dict, prompt_path: pathlib.Path, phase: str) -> str:
    ai_dir = str(meta["output_dir"])
    session_id = str(meta["session_id"])
    agent_dir = str(meta["agent_dir"])
    session_dir = str(meta["session_dir"])
    tab_id = str(meta.get("cdp_tab_id") or "")
    prompt_intent_id = tab_id or session_id
    prompt = build_prompt(
        prompt_path,
        session_id=prompt_intent_id,
        target_url=str(meta["target_url"]),
        ai_dir=ai_dir,
        group_id=str(meta["project_id"]),
        round_num=int(meta.get("round") or 0),
        total_rounds=int(meta.get("total_timeout_seconds") or 0) // max(1, int(meta.get("active_window_seconds") or 1)),
    )
    tools = FLUSH_TOOLS if phase in {"flush", "final_flush"} else CRAWL_TOOLS
    argv = _build_pi_argv(cfg=cfg, agent_dir=agent_dir, session_dir=session_dir, prompt=prompt, tools=tools)
    log_path = str(meta["log"])
    log_offset = pathlib.Path(log_path).stat().st_size if pathlib.Path(log_path).exists() else 0
    phase_started_at = utcnow()
    meta["phase"] = phase
    meta["status"] = "stopping" if meta.get("stop_requested") else "running"
    meta["phase_started_at"] = phase_started_at
    meta["phase_accepted"] = False
    write_meta(str(meta["project_id"]), meta)

    phase_result = "failed"
    with open(log_path, "a", encoding="utf-8") as log_f:
        log_f.write(f"\n=== phase {phase} start {phase_started_at} tab={tab_id or 'none'} ===\n")
        log_f.flush()
        proc = subprocess.Popen(
            argv,
            stdout=log_f,
            stderr=log_f,
            start_new_session=True,
        )
        meta["phase_pid"] = proc.pid
        write_meta(str(meta["project_id"]), meta)
        timeout = int(meta["flush_window_seconds"] if phase in {"flush", "final_flush"} else meta["active_window_seconds"])
        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=5)
        finally:
            meta["phase_pid"] = None
            increment = None if timed_out else _extract_phase_increment_payload(session_dir, log_path=log_path, log_offset=log_offset)
            if phase == "crawl":
                if increment is not None:
                    apply_crawler_increment(str(meta["project_id"]), increment)
                    merge_recent_traffic_into_state(meta)
                    meta["phase_accepted"] = True
                    meta["status"] = "running"
                    write_meta(str(meta["project_id"]), meta)
                    phase_result = "accepted"
                else:
                    meta["phase_accepted"] = False
                    meta["status"] = "failed"
                    write_meta(str(meta["project_id"]), meta)
                    phase_result = "needs_flush"
            else:
                if increment is not None:
                    apply_crawler_increment(str(meta["project_id"]), increment)
                    merge_recent_traffic_into_state(meta)
                    meta["phase_accepted"] = True
                    meta["last_flush_at"] = utcnow()
                    meta["status"] = "running"
                    write_meta(str(meta["project_id"]), meta)
                    phase_result = "accepted"
                elif fallback_persist_recent_traffic(meta, log_f):
                    meta["phase_accepted"] = True
                    meta["last_flush_at"] = utcnow()
                    meta["status"] = "running"
                    write_meta(str(meta["project_id"]), meta)
                    phase_result = "accepted"
                else:
                    meta["phase_accepted"] = False
                    meta["status"] = "failed"
                    write_meta(str(meta["project_id"]), meta)
                    phase_result = "failed"
    return phase_result


# ── Supervisor loop ────────────────────────

def run_crawler_supervisor(project_id: str) -> None:
    meta = read_meta(project_id)
    if not meta:
        return
    with open(str(meta["log"]), "a", encoding="utf-8") as log_f:
        log_f.write(f"\n=== supervisor start {utcnow()} project={project_id} ===\n")
    cfg = load_worker_config()

    tab_id = ensure_crawler_tab(meta)
    if tab_id:
        with open(str(meta["log"]), "a", encoding="utf-8") as log_f:
            log_f.write(f"=== crawler tab ready: {tab_id} ===\n")
    else:
        with open(str(meta["log"]), "a", encoding="utf-8") as log_f:
            log_f.write("=== WARNING: no cdp tab (Chrome on 9223 unreachable); crawl phases will fall back ===\n")

    started_at = meta.get("started_at")
    total_timeout = int(meta.get("total_timeout_seconds") or 0)
    total_deadline = time.time() + total_timeout
    if started_at:
        try:
            from datetime import datetime, timezone
            started = datetime.strptime(str(started_at), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            total_deadline = started.timestamp() + total_timeout
        except ValueError:
            pass

    try:
        while time.time() < total_deadline:
            meta = read_meta(project_id)
            if not meta:
                return
            if meta.get("stop_requested"):
                break
            if not ensure_crawler_tab(meta):
                with open(str(meta["log"]), "a", encoding="utf-8") as log_f:
                    log_f.write(f"\n=== supervisor loop round={meta.get('round')} no tab; abort crawl ===\n")
                break
            with open(str(meta["log"]), "a", encoding="utf-8") as log_f:
                log_f.write(f"\n=== supervisor loop round={meta.get('round')} phase=crawl ===\n")
            crawl_result = run_phase_once(cfg=cfg, meta=meta, prompt_path=CRAWLER_EXECUTE_PROMPT_PATH, phase="crawl")

            meta = read_meta(project_id)
            if not meta:
                return
            if crawl_result == "needs_flush":
                with open(str(meta["log"]), "a", encoding="utf-8") as log_f:
                    log_f.write(f"\n=== supervisor loop round={meta.get('round')} phase=flush ===\n")
                flush_result = run_phase_once(cfg=cfg, meta=meta, prompt_path=CRAWLER_FLUSH_PROMPT_PATH, phase="flush")
                if flush_result != "accepted":
                    meta = read_meta(project_id)
                    if meta:
                        meta["status"] = "failed"
                        meta["phase"] = "idle"
                        meta["pid"] = None
                        write_meta(project_id, meta)
                    return

            meta = read_meta(project_id)
            if not meta:
                return
            meta["round"] = int(meta.get("round") or 0) + 1
            write_meta(project_id, meta)
            if meta.get("stop_requested"):
                break
    finally:
        meta = read_meta(project_id)
        if meta:
            close_crawler_tab(str(meta.get("cdp_tab_id") or ""))
            meta["status"] = "done" if not meta.get("stop_requested") or bool(meta.get("phase_accepted")) else "stopping"
            meta["phase"] = "idle"
            meta["pid"] = None
            write_meta(project_id, meta)
