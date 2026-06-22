from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import PurePosixPath
from typing import Any

from axiom.dispatcher.config import WorkerConfig
from axiom.dispatcher.workers.base import DriverResult, WorkerDriver
from axiom.server.db import current_db_path

LOG = logging.getLogger(__name__)
DEFAULT_PI_TOOLS = ("read", "write", "edit", "bash", "grep", "find", "ls")


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
        if session:
            argv.extend(["--session", session])
        argv.extend(["-p", prompt])
        return DriverResult(argv=self._wrap_with_models(worker, argv, phase=_normalize_skill_phase(phase)), session=session)

    def build_conclude(self, worker: WorkerConfig, prompt: str, session: str, phase: str = "") -> list[str]:
        env = worker.env
        argv = [
            "--provider",
            "axiom",
            "--model",
            env["PI_MODEL"],
            "--session-dir",
            self._session_dir(worker),
            "--session",
            session,
            "-p",
            prompt,
        ]
        return self._wrap_with_models(worker, argv, phase=_normalize_skill_phase(phase))

    def extract_session(self, session: str | None, stdout: str, stderr: str) -> str | None:
        if session:
            return session
        for event in self._iter_events(stdout):
            if event.get("type") != "session":
                continue
            session_id = event.get("id")
            if isinstance(session_id, str) and session_id:
                return session_id
        return None

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
            raise RuntimeError("pi agent API error: " + "; ".join(unique_errors))

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

    def _wrap_with_models(self, worker: WorkerConfig, pi_argv: list[str], *, enable_tools: bool = True, **kwargs) -> list[str]:
        agent_dir = self._agent_dir(worker)
        skill_dir = str(PurePosixPath(agent_dir) / "skills")
        script = (
            'agent_dir="$1"\n'
            'models_json="$2"\n'
            'skill_dir="$3"\n'
            "shift 3\n"
            'mkdir -p "$agent_dir"\n'
            'mkdir -p "$agent_dir/sessions"\n'
            'printf "%s" "$models_json" > "$agent_dir/models.json"\n'
            'exec env PI_CODING_AGENT_DIR="$agent_dir" pi "$@"\n'
        )
        argv = [
            "--no-extensions",
            "--no-prompt-templates",
            "--no-themes",
            "--no-context-files",
        ]
        # Write skills to {skill_dir}/{name}/SKILL.md, then pass the whole
        # directory to pi.  pi recursively scans for SKILL.md and injects
        # <available_skills> XML into the system prompt.  The model reads
        # the full file on demand when the conversation matches.
        _write_skill_files(skill_dir)
        if _has_skill_files(skill_dir):
            argv.extend(["--skill", skill_dir])
        if enable_tools:
            argv.extend(["--tools", ",".join(_load_enabled_pi_tools())])
        return [
            "/bin/sh",
            "-lc",
            script,
            "--",
            agent_dir,
            self._models_json(worker),
            skill_dir,
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
            "apiKey": api_key,
            "models": [model],
        }
        payload = {"providers": {"axiom": provider}}
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


def _write_skill_files(skill_dir: str) -> None:
    """Write enabled skills to {skill_dir}/{name}/ for pi.

    For builtin skills: copy the entire source directory (including scripts/,
    references/, etc.) from dispatcher/skills/{name}/, then overwrite SKILL.md
    with DB content (so frontend edits take effect).

    For custom skills: write a single SKILL.md from DB content.

    pi requires --- frontmatter with name + description.
    """
    import os as _os
    import shutil as _shutil
    db_path = current_db_path()
    if not db_path.exists():
        return
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT name, description, content, source FROM pi_skills "
            "WHERE enabled = 1 AND content IS NOT NULL AND content != '' "
            "ORDER BY builtin DESC, name ASC"
        ).fetchall()
    except sqlite3.Error:
        conn.close()
        return
    conn.close()

    builtin_skills_dir = Path(__file__).resolve().parents[2] / "skills"

    for row in rows:
        name = row["name"].strip()
        desc = (row["description"] or "").strip()
        body = (row["content"] or "").strip()
        if not name or not body:
            continue
        skill_subdir = _os.path.join(skill_dir, name)
        _os.makedirs(skill_subdir, exist_ok=True)

        source_dir = builtin_skills_dir / name
        if row["source"] == "builtin" and source_dir.is_dir():
            for item in source_dir.iterdir():
                if item.name == "SKILL.md":
                    continue
                dest = Path(skill_subdir) / item.name
                if item.is_dir():
                    if dest.exists():
                        _shutil.rmtree(dest)
                    _shutil.copytree(item, dest)
                else:
                    _shutil.copy2(item, dest)

        path = _os.path.join(skill_subdir, "SKILL.md")
        if body.startswith("---"):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
        else:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(f"---\nname: {name}\ndescription: {desc}\n---\n\n{body}")


def _has_skill_files(skill_dir: str) -> bool:
    import os as _os
    if not _os.path.isdir(skill_dir):
        return False
    for entry in _os.scandir(skill_dir):
        if entry.is_dir():
            if _os.path.isfile(_os.path.join(entry.path, "SKILL.md")):
                return True
    return False


def _load_enabled_pi_tools() -> tuple[str, ...]:
    db_path = current_db_path()
    if not db_path.exists():
        return DEFAULT_PI_TOOLS
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT name FROM pi_tools WHERE enabled = 1 ORDER BY builtin DESC, name ASC"
        ).fetchall()
    except sqlite3.Error as exc:
        LOG.warning("failed to read PI tools, using defaults error=%s", exc)
        return DEFAULT_PI_TOOLS
    finally:
        conn.close()
    names = tuple(row["name"] for row in rows if row["name"])
    return names or DEFAULT_PI_TOOLS
