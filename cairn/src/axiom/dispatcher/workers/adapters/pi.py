from __future__ import annotations

import json
import logging
import os
import sqlite3
import shutil
import subprocess
import hashlib
from pathlib import Path, PurePosixPath
from typing import Any

from axiom.dispatcher.config import WorkerConfig
from axiom.dispatcher.workers.base import DriverResult, WorkerDriver
from axiom.server.db import current_db_path

LOG = logging.getLogger(__name__)
DEFAULT_PI_TOOLS = ("read", "write", "bash", "edit")
MCP_TOOL_NAME = "mcp"
MCP_CONFIG_FILE = "mcp.json"
MCP_ADAPTER_INDEX = "/Users/hexin/.pi/agent/extensions/pi-mcp-adapter/index.ts"
MCP_ADAPTER_SETTINGS_FILE = "settings.json"
AXIOM_GUARD_EXTENSION_INDEX = str(Path(__file__).resolve().parents[2] / "extensions" / "axiom-guard" / "index.ts")
PI_THINKING_LEVELS = {"off", "minimal", "low", "medium", "high", "xhigh"}


def _normalize_skill_phase(phase: str) -> str:
    """Map full phase strings (bootstrap_healthcheck, reason_execute, etc.) to base phases."""
    for base in ("bootstrap", "reason", "explore"):
        if phase.startswith(base):
            return base
    return phase


def _load_enabled_pi_skills(phase: str = "") -> tuple[str, ...]:
    db_path = current_db_path()
    if not db_path.exists():
        return ()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT skill_path, phases_json FROM pi_skills WHERE enabled = 1 ORDER BY builtin DESC, name ASC"
        ).fetchall()
    except sqlite3.Error:
        return ()
    finally:
        conn.close()
    skills: list[str] = []
    for row in rows:
        if not row["skill_path"]:
            continue
        if phase:
            try:
                phases = json.loads(row["phases_json"] or "[]")
            except json.JSONDecodeError:
                phases = []
            if not isinstance(phases, list) or phase not in phases:
                continue
        skills.append(row["skill_path"])
    return tuple(skills)


class PiDriver(WorkerDriver):
    type_name = "pi"
    _session_id_supported: dict[str, bool] = {}

    def prepare_session(self) -> str | None:
        # Pre-allocate a deterministic session id per task execution so
        # conclude fallback can reliably resume the same intent session
        # even when execute phase times out and stdout is empty.
        import uuid as _uuid
        return str(_uuid.uuid4())

    def build_healthcheck(self, worker: WorkerConfig, phase: str = "") -> list[str]:
        env = worker.env
        return self._wrap_with_models(
            worker,
            [
                "--provider",
                "axiom",
                "--model",
                env["PI_MODEL"],
                "--session-dir",
                self._session_dir(worker),
                "--no-session",
                "--no-tools",
                "-p",
                "Reply with exactly pong.",
            ],
            enable_tools=False,
            phase=_normalize_skill_phase(phase),
            force_disable_mcp=True,
        )

    def build_execute(self, worker: WorkerConfig, prompt: str, session: str | None, phase: str = "") -> DriverResult:
        env = worker.env
        argv = [
            "--provider",
            "axiom",
            "--model",
            env["PI_MODEL"],
            "--session-dir",
            self._session_dir(worker),
        ]
        # PI >= 0.76 supports --session-id for deterministic create/resume.
        # Fallback to legacy --session only when the session file already exists.
        if session:
            if self._supports_session_id(worker):
                argv.extend(["--session-id", session])
            elif self._session_file_exists(worker, session):
                argv.extend(["--session", session])
        self._append_thinking(worker, argv)
        argv.extend(["-p", prompt])
        return DriverResult(argv=self._wrap_with_models(worker, argv, phase=_normalize_skill_phase(phase)), session=session)

    def build_conclude(self, worker: WorkerConfig, prompt: str, session: str, phase: str = "") -> list[str]:
        env = worker.env
        # Try to find the actual session ID that PI created during execute.
        # The pre-allocated UUID may not match if execute didn't use --session.
        actual_session = session
        if not self._session_file_exists(worker, session):
            fallback = self.find_session_from_files(worker.name)
            if fallback:
                actual_session = fallback
        session_flag = "--session-id" if self._supports_session_id(worker) else "--session"
        argv = [
            "--provider",
            "axiom",
            "--model",
            env["PI_MODEL"],
            "--session-dir",
            self._session_dir(worker),
            session_flag,
            actual_session,
        ]
        self._append_thinking(worker, argv)
        argv.extend(["-p", prompt])
        return self._wrap_with_models(worker, argv, phase=_normalize_skill_phase(phase))

    @staticmethod
    def _append_thinking(worker: WorkerConfig, argv: list[str]) -> None:
        level = (worker.env.get("PI_THINKING") or "").strip().lower()
        if not level or level == "off":
            return
        if level not in PI_THINKING_LEVELS:
            return
        argv.extend(["--thinking", level])

    def extract_session(self, session: str | None, stdout: str, stderr: str) -> str | None:
        # Try to parse the actual session ID from PI stdout first.
        # This handles the case where PI auto-generated a session (because
        # the pre-allocated UUID didn't have a file yet).
        for event in self._iter_events(stdout):
            if event.get("type") != "session":
                continue
            session_id = event.get("id")
            if isinstance(session_id, str) and session_id:
                return session_id
        # Fall back only when the pre-allocated session ID actually exists.
        # This prevents using a UUID that PI never created.
        if session and self._session_id_exists_globally(session):
            return session
        return None

    def session_exists(self, worker: WorkerConfig, session_id: str | None) -> bool:
        if not session_id:
            return False
        return self._session_file_exists(worker, session_id)

    @staticmethod
    def _session_file_exists(worker: WorkerConfig, session_id: str) -> bool:
        """Check if a session file exists in the worker's session directory."""
        from pathlib import Path as _Path
        session_dir = _Path("/tmp/axiom-pi") / worker.name / "sessions"
        # PI stores sessions as <timestamp>_<uuid>.jsonl
        matches = list(session_dir.glob(f"*{session_id}*.jsonl"))
        return len(matches) > 0

    @staticmethod
    def _session_id_exists_globally(session_id: str) -> bool:
        """Check whether any PI worker session file contains this session ID."""
        root = Path("/tmp/axiom-pi")
        if not root.exists():
            return False
        for candidate in root.glob(f"*/sessions/*{session_id}*.jsonl"):
            if candidate.is_file():
                return True
        return False

    @staticmethod
    def find_session_from_files(worker_name: str) -> str | None:
        """Fallback: extract session ID from the newest session file.
        Used when stdout is empty (e.g. after a timeout kill)."""
        import os as _os
        from pathlib import Path as _Path
        session_dir = _Path("/tmp/axiom-pi") / worker_name / "sessions"
        if not session_dir.exists():
            return None
        candidates = sorted(
            [p for p in session_dir.iterdir() if p.suffix == ".jsonl"],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            # Filename format: <timestamp>_<uuid>.jsonl
            name = candidates[0].stem
            parts = name.split("_", 1)
            return parts[1] if len(parts) > 1 else None
        return None

    @classmethod
    def _resolve_pi_path(cls, worker: "WorkerConfig | None" = None) -> str:
        """Resolve the absolute path to the pi binary.

        Precedence (explicit path wins over PATH lookup):
          1. ``PI_BINARY_PATH`` in the worker env
          2. ``PI_BINARY_PATH`` in the dispatcher process environment
          3. ``shutil.which("pi")`` (PATH lookup — may be stale, last resort)
        """
        candidates: list[str] = []
        if worker is not None:
            explicit = (worker.env or {}).get("PI_BINARY_PATH")
            if explicit:
                candidates.append(explicit)
        env_path = os.environ.get("PI_BINARY_PATH")
        if env_path:
            candidates.append(env_path)
        for candidate in candidates:
            if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        resolved = shutil.which("pi")
        if resolved:
            return resolved
        return "pi"

    @classmethod
    def _supports_session_id(cls, worker: "WorkerConfig | None" = None) -> bool:
        pi_path = cls._resolve_pi_path(worker)
        cached = cls._session_id_supported.get(pi_path)
        if cached is not None:
            return cached
        try:
            result = subprocess.run(
                [pi_path, "--help"],
                capture_output=True,
                text=True,
                check=False,
                timeout=3,
            )
            output = (result.stdout or "") + "\n" + (result.stderr or "")
            cls._session_id_supported[pi_path] = "--session-id" in output
        except Exception:
            cls._session_id_supported[pi_path] = False
        return cls._session_id_supported[pi_path]

    def extract_response_text(self, stdout: str, stderr: str) -> str:
        assistant_message: dict[str, Any] | None = None
        text_blocks: dict[int, str] = {}
        text_deltas: dict[int, list[str]] = {}
        assistant_errors: list[str] = []
        assistant_stop_errors: list[str] = []
        for event in self._iter_events(stdout):
            event_type = event.get("type")
            if event_type == "turn_end":
                message = event.get("message")
                if isinstance(message, dict) and message.get("role") == "assistant":
                    assistant_message = message
                    if message.get("stopReason") == "error":
                        err = message.get("errorMessage")
                        if isinstance(err, str) and err.strip():
                            assistant_stop_errors.append(err.strip())
            elif event_type == "agent_end":
                messages = event.get("messages")
                if isinstance(messages, list):
                    for message in reversed(messages):
                        if isinstance(message, dict) and message.get("role") == "assistant":
                            assistant_message = message
                            if message.get("stopReason") == "error":
                                err = message.get("errorMessage")
                                if isinstance(err, str) and err.strip():
                                    assistant_stop_errors.append(err.strip())
                            break
            elif event_type in {"message_start", "message_end", "message_update"}:
                message = event.get("message")
                if isinstance(message, dict) and message.get("role") == "assistant":
                    assistant_message = message
                    if message.get("stopReason") == "error":
                        err = message.get("errorMessage")
                        if isinstance(err, str) and err.strip():
                            assistant_stop_errors.append(err.strip())
                assistant_event = event.get("assistantMessageEvent")
                if isinstance(assistant_event, dict):
                    partial = assistant_event.get("partial")
                    if isinstance(partial, dict) and partial.get("role") == "assistant":
                        assistant_message = partial
                    nested_event_type = assistant_event.get("type")
                    if nested_event_type == "text_end":
                        content_index = assistant_event.get("contentIndex")
                        content = assistant_event.get("content")
                        if isinstance(content_index, int) and isinstance(content, str):
                            text_blocks[content_index] = content
                    elif nested_event_type == "text_delta":
                        content_index = assistant_event.get("contentIndex")
                        delta = assistant_event.get("delta")
                        if isinstance(content_index, int) and isinstance(delta, str) and delta:
                            text_deltas.setdefault(content_index, []).append(delta)
            elif event_type == "done":
                message = event.get("message")
                if isinstance(message, dict) and message.get("role") == "assistant":
                    assistant_message = message
            elif event_type == "start":
                partial = event.get("partial")
                if isinstance(partial, dict) and partial.get("role") == "assistant":
                    assistant_message = partial
            elif event_type == "text_end":
                content_index = event.get("contentIndex")
                content = event.get("content")
                if isinstance(content_index, int) and isinstance(content, str):
                    text_blocks[content_index] = content
            elif event_type == "text_delta":
                content_index = event.get("contentIndex")
                delta = event.get("delta")
                if isinstance(content_index, int) and isinstance(delta, str) and delta:
                    text_deltas.setdefault(content_index, []).append(delta)
            message = event.get("message")
            if isinstance(message, dict) and message.get("role") == "assistant":
                error_message = message.get("errorMessage")
                if isinstance(error_message, str) and error_message.strip():
                    assistant_errors.append(error_message.strip())

        # Fast-fail: if the model never produced any text content and all
        # attempts ended with stopReason=error, surface the API error clearly
        # instead of returning error strings that will fail JSON parsing.
        message_text = self._extract_text_from_message(assistant_message)
        if not message_text and not text_blocks and not text_deltas and assistant_stop_errors:
            unique_errors = list(dict.fromkeys(assistant_stop_errors))
            return "\n".join(unique_errors)

        if not message_text and not text_blocks and not text_deltas and assistant_errors:
            unique_errors = list(dict.fromkeys(assistant_errors))
            return "\n".join(unique_errors)

        if message_text:
            return message_text

        # Some reasoning-focused models put all their output in
        # reasoning_content and leave the text content block empty.  Fall back
        # to reasoning_content when the text content is blank.
        reasoning_text = self._extract_reasoning_from_message(assistant_message)
        if reasoning_text:
            return reasoning_text

        if text_blocks or text_deltas:
            indexes = sorted(set(text_blocks) | set(text_deltas))
            parts: list[str] = []
            for index in indexes:
                if index in text_blocks:
                    parts.append(text_blocks[index])
                elif index in text_deltas:
                    parts.append("".join(text_deltas[index]))
            joined = "\n".join(part for part in parts if part).strip()
            if joined:
                return joined

        # Fast-fail: the model returned zero output tokens with empty content.
        # This usually means a safety filter blocked the response, or the API
        # returned a stop without producing any text.  Don't fall through to
        # event scanning which would pick up JSON templates from the prompt.
        if isinstance(assistant_message, dict) and not message_text and not text_blocks and not text_deltas:
            usage = assistant_message.get("usage")
            if isinstance(usage, dict):
                output_tokens = usage.get("output_tokens", usage.get("outputTokens", usage.get("output", 0)))
                if output_tokens is not None and int(output_tokens) == 0:
                    stop_reason = assistant_message.get("stopReason", "unknown")
                    raise RuntimeError(
                        f"pi agent returned empty response (stopReason={stop_reason}, outputTokens=0)"
                    )
            content = assistant_message.get("content")
            if isinstance(content, list) and len(content) == 0:
                stop_reason = assistant_message.get("stopReason", "unknown")
                # Only raise if we also have no errors — if there are errors
                # we'd have returned them above via assistant_errors.
                if not assistant_errors:
                    raise RuntimeError(
                        f"pi agent returned empty response (stopReason={stop_reason}, content=[])"
                    )

        if assistant_errors:
            return "\n".join(assistant_errors)

        fallback = self._extract_text_from_events(self._iter_events(stdout))
        if fallback:
            return fallback

        return stdout

    def _wrap_with_models(self, worker: WorkerConfig, pi_argv: list[str], *, enable_tools: bool = True, force_disable_mcp: bool = False, **kwargs) -> list[str]:
        agent_dir = self._agent_dir(worker)
        skill_dir = str(PurePosixPath(agent_dir) / "skills")
        mcp_enabled = _is_truthy(worker.env.get("PI_MCP_ENABLED", "")) if not force_disable_mcp else False
        mcp_config = _resolve_worker_mcp_config(worker) if not force_disable_mcp else None
        phase = str(kwargs.get("phase") or "")
        guard_enabled = _guard_enabled_for_phase(worker, phase)
        script = (
            'agent_dir="$1"\n'
            'models_json="$2"\n'
            'skill_dir="$3"\n'
            'pi_exec="$4"\n'
            "shift 4\n"
            'mkdir -p "$agent_dir"\n'
            'mkdir -p "$agent_dir/sessions"\n'
            'printf "%s" "$models_json" > "$agent_dir/models.json"\n'
            'exec env PI_CODING_AGENT_DIR="$agent_dir" "$pi_exec" "$@"\n'
        )
        argv = [
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
            "--no-context-files",
        ]
        extension_paths: list[str] = []
        if mcp_enabled and mcp_config is not None:
            extension_paths.append(MCP_ADAPTER_INDEX)
        if guard_enabled:
            extension_paths.append(AXIOM_GUARD_EXTENSION_INDEX)
        if not extension_paths:
            argv.insert(0, "--no-extensions")
        # Write skills to {skill_dir}/{name}/SKILL.md, then pass the whole
        # directory to pi.  pi recursively scans for SKILL.md and injects
        # <available_skills> XML into the system prompt.  The model reads
        # the full file on demand when the conversation matches.
        _write_skill_files(skill_dir)
        if mcp_enabled and mcp_config is not None:
            mcp_config_path = _write_worker_mcp_config(agent_dir, mcp_config, extension_paths=extension_paths)
            argv.extend(["--mcp-config", mcp_config_path])
        else:
            _clear_worker_mcp_config(agent_dir)
            _write_worker_extension_settings(agent_dir, extension_paths)
        if _has_skill_files(skill_dir):
            argv.extend(["--skill", skill_dir])
        if enable_tools:
            argv.extend(["--tools", ",".join(_load_enabled_pi_tools(worker))])
        return [
            "/bin/sh",
            "-lc",
            script,
            "--",
            agent_dir,
            self._models_json(worker),
            skill_dir,
            self._resolve_pi_path(worker),
            *argv,
            *pi_argv,
        ]

    @staticmethod
    def _agent_dir(worker: WorkerConfig) -> str:
        return str(PurePosixPath("/tmp/axiom-pi") / worker.name)

    @staticmethod
    def _session_dir(worker: WorkerConfig) -> str:
        return str(PurePosixPath(PiDriver._agent_dir(worker)) / "sessions")

    @staticmethod
    def _iter_events(stdout: str) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                events.append(payload)
        return events

    @staticmethod
    def _extract_reasoning_from_message(message: dict[str, Any] | None) -> str:
        """Extract reasoning_content from an assistant message as a fallback."""
        if not isinstance(message, dict):
            return ""
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return ""  # text content is present, no need to fall back
        reasoning = message.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning.strip():
            return reasoning.strip()
        return ""

    @staticmethod
    def _extract_text_from_message(message: dict[str, Any] | None) -> str:
        if not isinstance(message, dict):
            return ""
        content = message.get("content")
        if not isinstance(content, list):
            return ""
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "text":
                continue
            text = item.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
        return "\n".join(parts).strip()

    @classmethod
    def _extract_text_from_events(cls, events: list[dict[str, Any]]) -> str:
        candidates: list[str] = []
        for event in events:
            cls._collect_text_candidates(event, candidates)
        if not candidates:
            return ""
        scored: list[tuple[int, int, str]] = []
        for index, candidate in enumerate(candidates):
            score = 0
            lowered = candidate.lower()
            if "```json" in lowered:
                score += 4
            if '"accepted"' in lowered:
                score += 3
            if '"intents"' in lowered or '"complete"' in lowered or '"vulns"' in lowered:
                score += 2
            if candidate.strip().startswith("{"):
                score += 1
            scored.append((score, index, candidate))
        scored.sort(key=lambda item: (item[0], item[1], len(item[2])))
        return scored[-1][2].strip()

    @classmethod
    def _collect_text_candidates(cls, value: Any, candidates: list[str]) -> None:
        if isinstance(value, dict):
            # Skip user messages — they contain the prompt template whose
            # JSON examples must not be mistaken for the model's output.
            if value.get("role") == "user":
                return
            text = value.get("text")
            if isinstance(text, str) and text.strip():
                candidates.append(text)
            content = value.get("content")
            if isinstance(content, str) and content.strip():
                candidates.append(content)
            for nested in value.values():
                cls._collect_text_candidates(nested, candidates)
        elif isinstance(value, list):
            for item in value:
                cls._collect_text_candidates(item, candidates)

    @staticmethod
    def _models_json(worker: WorkerConfig) -> str:
        env = worker.env
        model: dict[str, Any] = {
            "id": env["PI_MODEL"],
            "name": env["PI_MODEL"],
        }
        context_window = env.get("PI_MODEL_CONTEXT_WINDOW")
        if context_window:
            model["contextWindow"] = int(context_window)

        api_key = env.get("PI_API_KEY", "")
        # Never pass placeholder values as real API keys.
        if not api_key or api_key.startswith("<"):
            api_key = ""

        provider: dict[str, Any] = {
            "baseUrl": env["PI_BASE_URL"],
            "api": env["PI_PROVIDER_API"],
            "models": [model],
        }
        if api_key:
            provider["apiKey"] = api_key
        else:
            provider["apiKey"] = "none"  # pi requires at least 1 char
        payload = {"providers": {"axiom": provider}}
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


def _write_skill_files(skill_dir: str) -> None:
    """Copy all skills from the source directory to the worker's skill directory.

    Scans dispatcher/skills/{name}/SKILL.md, copies the entire directory
    (including scripts/, references/, etc.) for each skill that is not
    disabled (no disable-model-invocation: true in frontmatter).
    """
    import os as _os
    import shutil as _shutil

    source_dir = Path(__file__).resolve().parents[2] / "skills"
    if not source_dir.is_dir():
        return

    # Clean old skill dir
    if _os.path.isdir(skill_dir):
        for entry in _os.scandir(skill_dir):
            if entry.is_dir():
                _shutil.rmtree(entry.path)

    for skill_entry in sorted(source_dir.iterdir()):
        if not skill_entry.is_dir():
            continue
        skill_file = skill_entry / "SKILL.md"
        if not skill_file.exists():
            continue

        # Check if disabled via frontmatter
        raw = skill_file.read_text(encoding="utf-8")
        if "disable-model-invocation: true" in raw:
            continue

        name = skill_entry.name
        dest = Path(skill_dir) / name
        _shutil.copytree(skill_entry, dest)


def _has_skill_files(skill_dir: str) -> bool:
    import os as _os
    if not _os.path.isdir(skill_dir):
        return False
    for entry in _os.scandir(skill_dir):
        if entry.is_dir():
            if _os.path.isfile(_os.path.join(entry.path, "SKILL.md")):
                return True
    return False


def _load_enabled_pi_tools(worker: WorkerConfig | None = None) -> tuple[str, ...]:
    mcp_enabled = bool(worker and _is_truthy(worker.env.get("PI_MCP_ENABLED", "")))
    if worker and _is_truthy(worker.env.get("AXIOM_V2", "")):
        raw = worker.env.get("AXIOM_PI_TOOLS_JSON", "")
        try:
            configured = json.loads(raw) if raw else list(DEFAULT_PI_TOOLS)
        except json.JSONDecodeError:
            configured = list(DEFAULT_PI_TOOLS)
        names = tuple(str(item) for item in configured if str(item).strip())
        normalized = [name for name in names if name != MCP_TOOL_NAME]
        if mcp_enabled:
            normalized.append(MCP_TOOL_NAME)
        return tuple(dict.fromkeys(normalized))
    db_path = current_db_path()
    names: tuple[str, ...] = ()
    if not db_path.exists():
        names = DEFAULT_PI_TOOLS
    else:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT name FROM pi_tools WHERE enabled = 1 ORDER BY builtin DESC, name ASC"
            ).fetchall()
        except sqlite3.Error as exc:
            LOG.warning("failed to read PI tools, using defaults error=%s", exc)
            names = DEFAULT_PI_TOOLS
        else:
            names = tuple(row["name"] for row in rows if row["name"]) or DEFAULT_PI_TOOLS
        finally:
            conn.close()
    normalized = [name for name in names if name and name != MCP_TOOL_NAME]
    if mcp_enabled:
        normalized.append(MCP_TOOL_NAME)
    return tuple(dict.fromkeys(normalized))


def _is_truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _parse_csv_set(raw: str) -> set[str]:
    return {part.strip() for part in raw.split(",") if part.strip()}


def _split_allowed_mcp_tools(tokens: set[str]) -> tuple[set[str], dict[str, set[str]]]:
    flat: set[str] = set()
    scoped: dict[str, set[str]] = {}
    for token in tokens:
        server = ""
        tool = ""
        if ":" in token:
            server, tool = token.split(":", 1)
        elif "/" in token:
            server, tool = token.split("/", 1)
        else:
            flat.add(token)
            continue
        server = server.strip()
        tool = tool.strip()
        if server and tool:
            scoped.setdefault(server, set()).add(tool)
    return flat, scoped


def _load_global_mcp_servers() -> dict[str, Any]:
    """Load enabled global MCP servers from DB and emit pi-mcp-adapter-compatible
    entries (with ``mcpServers``/entry shape: command+args+env or url).

    Backend url-only entries (command empty, url present) get emitted as
    ``{"url": "<url>"}``.  stdio entries become
    ``{"command": "<cmd>", "args": [...], "env": {...}}``.
    """
    db_path = current_db_path()
    if not db_path.exists():
        return {}
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    servers: dict[str, Any] = {}
    try:
        try:
            rows = conn.execute(
                "SELECT id, name, command, url, args_json, env_json FROM mcp_servers WHERE disabled = 0"
            ).fetchall()
        except sqlite3.Error:
            # Pre-migration DB without url column
            rows = conn.execute(
                "SELECT id, name, command, args_json, env_json FROM mcp_servers WHERE disabled = 0"
            ).fetchall()
        tool_rows_by_server: dict[str, list[sqlite3.Row]] = {}
        server_ids = [row["id"] for row in rows if "id" in row.keys() and row["id"]]
        if server_ids:
            try:
                placeholders = ",".join("?" for _ in server_ids)
                tool_rows = conn.execute(
                    f"SELECT server_id, name, enabled FROM mcp_server_tools WHERE server_id IN ({placeholders}) ORDER BY name ASC",
                    server_ids,
                ).fetchall()
                for tool_row in tool_rows:
                    tool_rows_by_server.setdefault(tool_row["server_id"], []).append(tool_row)
            except sqlite3.Error as exc:
                LOG.warning("failed to read global mcp server tools: %s", exc)
        for row in rows:
            name = row["name"]
            if not name:
                continue
            server_id = row["id"] if "id" in row.keys() else ""
            url = row["url"] if "url" in row.keys() else None
            try:
                args = json.loads(row["args_json"])
                env = json.loads(row["env_json"])
            except json.JSONDecodeError:
                continue
            entry: dict[str, Any] = {}
            if url:
                entry["url"] = url
            elif row["command"]:
                if not isinstance(args, list) or not isinstance(env, dict):
                    continue
                entry["command"] = row["command"]
                entry["args"] = args
                entry["env"] = env
            else:
                continue
            configured_tools = tool_rows_by_server.get(server_id, [])
            if configured_tools:
                entry["directTools"] = [
                    tool_row["name"]
                    for tool_row in configured_tools
                    if tool_row["name"] and int(tool_row["enabled"]) == 1
                ]
            servers[name] = entry
    except sqlite3.Error as exc:
        LOG.warning("failed to read global mcp servers: %s", exc)
    finally:
        conn.close()
    return servers


def _resolve_worker_mcp_config(worker: WorkerConfig) -> dict[str, Any] | None:
    """Resolve worker MCP config from env JSON with allowlist filtering."""
    raw = (worker.env.get("PI_MCP_CONFIG_JSON") or "").strip()
    if not _is_truthy(worker.env.get("PI_MCP_ENABLED", "")):
        return None

    parsed = {"mcpServers": {}, "settings": {"toolPrefix": "server"}}
    if raw:
        try:
            user_parsed = json.loads(raw)
            if isinstance(user_parsed, dict):
                parsed = user_parsed
        except json.JSONDecodeError as exc:
            LOG.warning("worker=%s invalid PI_MCP_CONFIG_JSON: %s", worker.name, exc)

    mcp_servers = parsed.get("mcpServers")
    if not isinstance(mcp_servers, dict):
        parsed["mcpServers"] = {}
        mcp_servers = parsed["mcpServers"]

    # Inject global servers
    global_servers = _load_global_mcp_servers()
    for name, cfg in global_servers.items():
        if name not in mcp_servers:
            mcp_servers[name] = cfg

    allowed_servers = _parse_csv_set(worker.env.get("PI_MCP_ALLOWED_SERVERS", ""))
    if allowed_servers:
        mcp_servers = {name: cfg for name, cfg in mcp_servers.items() if name in allowed_servers}
        parsed["mcpServers"] = mcp_servers

    allowed_tools = _parse_csv_set(worker.env.get("PI_MCP_ALLOWED_TOOLS", ""))
    if allowed_tools:
        flat_allowed_tools, scoped_allowed_tools = _split_allowed_mcp_tools(allowed_tools)
        for server_name, server_cfg in list(mcp_servers.items()):
            if not isinstance(server_cfg, dict):
                mcp_servers[server_name] = {}
                continue
            server_allowed_tools = set(flat_allowed_tools)
            server_allowed_tools.update(scoped_allowed_tools.get(server_name, set()))
            direct_tools = server_cfg.get("directTools")
            if direct_tools is True:
                # Block full tool exposure when allowlist exists.
                server_cfg["directTools"] = sorted(server_allowed_tools)
            elif isinstance(direct_tools, list):
                server_cfg["directTools"] = [
                    tool for tool in direct_tools if isinstance(tool, str) and tool in server_allowed_tools
                ]
            elif server_allowed_tools:
                server_cfg["directTools"] = sorted(server_allowed_tools)
    return parsed


def _guard_enabled_for_phase(worker: WorkerConfig, phase: str) -> bool:
    normalized = _normalize_skill_phase(phase or "")
    if normalized not in {"bootstrap", "explore"}:
        return False
    # Keep loading the extension for legacy scoped workers until the legacy
    # dispatcher is removed. V2 always sets AXIOM_TASK_SCOPE_ENABLED=0, so
    # Scope never becomes a network guard in the new runtime.
    return _is_truthy(worker.env.get("AXIOM_GUARD_ENABLED", "")) or _is_truthy(
        worker.env.get("AXIOM_TASK_SCOPE_ENABLED", "")
    )


def _write_worker_extension_settings(agent_dir: str, extension_paths: list[str]) -> None:
    settings_path = Path(agent_dir) / MCP_ADAPTER_SETTINGS_FILE
    if not extension_paths:
        if settings_path.exists():
            settings_path.unlink()
        return
    unique_paths: list[str] = []
    for path in extension_paths:
        if path and path not in unique_paths:
            unique_paths.append(path)
    settings = {"extensions": unique_paths}
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, ensure_ascii=True), encoding="utf-8")
    LOG.info("registered pi extension settings path=%s extensions=%s", settings_path, unique_paths)


def _write_worker_mcp_config(agent_dir: str, config: dict[str, Any], *, extension_paths: list[str] | None = None) -> str:
    """Write per-worker mcp.json (mcpServers shape) and settings.json (with
    pi-mcp-adapter extension registered).  pi-mcp-adapter reads both files
    relative to ``PI_CODING_AGENT_DIR``, so each worker gets its own config.
    """
    agent_path = Path(agent_dir)
    agent_path.mkdir(parents=True, exist_ok=True)

    payload = json.dumps(config, ensure_ascii=True, separators=(",", ":"))
    mcp_path = agent_path / MCP_CONFIG_FILE
    mcp_path.write_text(payload, encoding="utf-8")
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
    LOG.info("prepared worker mcp config path=%s sha256=%s", mcp_path, digest)

    _write_worker_extension_settings(agent_dir, extension_paths or [MCP_ADAPTER_INDEX])
    LOG.info("registered pi extension paths=%s", extension_paths or [MCP_ADAPTER_INDEX])
    return str(mcp_path)


def _clear_worker_mcp_config(agent_dir: str) -> None:
    agent_path = Path(agent_dir)
    for filename in (MCP_CONFIG_FILE, "mcp-cache.json"):
        path = agent_path / filename
        if path.exists():
            path.unlink()
