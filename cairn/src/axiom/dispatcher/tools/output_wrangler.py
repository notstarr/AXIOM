from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

LOG = logging.getLogger(__name__)

DEFAULT_THRESHOLD_BYTES = 4096
DEFAULT_PREVIEW_LINES = 30
DEFAULT_KEEP_HEAD = 20
DEFAULT_KEEP_TAIL = 10
MAX_SAFE_SUMMARY_BYTES = 3500

_SAFE_NAME = re.compile(r"[^a-z0-9_-]+")


@dataclass(slots=True)
class WrangledOutput:
    text: str
    truncated: bool
    artifact_path: str | None
    original_bytes: int


class OutputWrangler:
    """Replace tool stdout/stderr with a pointer + summary when oversized.

    The wrangler is a passive middleware that lives between ``process.communicate``
    and the rest of the dispatcher.  Its only job is to keep large tool results
    out of the LLM context while still letting the model see enough to know what
    happened and where the full content lives.

    It does NOT modify any DB schema, prompt template, dispatcher loop, or worker
    driver — only the ``stdout`` / ``stderr`` on a ``ProcessResult`` that has
    already left the subprocess.
    """

    def __init__(
        self,
        *,
        threshold_bytes: int = DEFAULT_THRESHOLD_BYTES,
        preview_lines: int = DEFAULT_PREVIEW_LINES,
        keep_head: int = DEFAULT_KEEP_HEAD,
        keep_tail: int = DEFAULT_KEEP_TAIL,
    ) -> None:
        self._threshold = int(threshold_bytes)
        self._preview_lines = int(preview_lines)
        self._keep_head = int(keep_head)
        self._keep_tail = int(keep_tail)

    def wrangle_stdout(
        self,
        *,
        result_stdout: str,
        invocation_tool: str,
        argv: list[str],
        phase: str,
        ai_dir: str | None,
    ) -> tuple[str, bool, str | None]:
        """Wrap ``result.stdout`` if needed.  Returns (new_text, was_truncated, artifact_path)."""
        return self._wrangle_text(
            text=result_stdout,
            tag="stdout",
            invocation_tool=invocation_tool,
            argv=argv,
            phase=phase,
            ai_dir=ai_dir,
        )

    def wrangle_stderr(
        self,
        *,
        result_stderr: str,
        invocation_tool: str,
        argv: list[str],
        phase: str,
        ai_dir: str | None,
    ) -> tuple[str, bool, str | None]:
        """Wrap ``result.stderr`` if needed.  Symmetric to ``wrangle_stdout``."""
        return self._wrangle_text(
            text=result_stderr,
            tag="stderr",
            invocation_tool=invocation_tool,
            argv=argv,
            phase=phase,
            ai_dir=ai_dir,
        )

    def _wrangle_text(
        self,
        *,
        text: str,
        tag: str,
        invocation_tool: str,
        argv: list[str],
        phase: str,
        ai_dir: str | None,
    ) -> tuple[str, bool, str | None]:
        if text is None:
            return "", False, None
        if len(text.encode("utf-8", errors="replace")) <= self._threshold:
            return text, False, None

        artifact_path = self._write_artifact(
            text=text,
            tag=tag,
            tool=invocation_tool,
            argv=argv,
            phase=phase,
            ai_dir=ai_dir,
        )
        summary = self._make_summary(text)
        pointer = (
            f"[output truncated: {len(text)} chars / {len(text.encode('utf-8', errors='replace'))} bytes "
            f"→ artifact {artifact_path}]"
        )
        wrapped = f"{pointer}\n\n{summary}\n"
        if len(wrapped.encode("utf-8", errors="replace")) > MAX_SAFE_SUMMARY_BYTES:
            wrapped = wrapped[:MAX_SAFE_SUMMARY_BYTES] + "\n... (summary truncated)"
        return wrapped, True, artifact_path

    def _write_artifact(
        self,
        *,
        text: str,
        tag: str,
        tool: str,
        argv: list[str],
        phase: str,
        ai_dir: str | None,
    ) -> str:
        target_dir = self._artifact_dir(ai_dir=ai_dir, phase=phase)
        ts_ms = int(time.time() * 1000)
        digest = hashlib.md5(text.encode("utf-8", errors="replace")).hexdigest()[:8]
        tool_safe = _SAFE_NAME.sub("_", (tool or "unknown").lower())[:32] or "tool"
        filename = f"{tool_safe}_{tag}_{ts_ms}_{digest}.md"
        path = target_dir / filename
        target_dir.mkdir(parents=True, exist_ok=True)
        argv_preview = " ".join(self._sanitize_argv_token(tok) for tok in argv[:32])
        if len(argv) > 32:
            argv_preview += f" ... (+{len(argv) - 32} more)"
        body = (
            f"# tool={tool} tag={tag} phase={phase}\n\n"
            f"## argv\n```\n{argv_preview}\n```\n\n"
            f"## output ({len(text)} chars)\n```\n{text}\n```\n"
        )
        path.write_text(body, encoding="utf-8")
        return str(path)

    def _artifact_dir(self, *, ai_dir: str | None, phase: str) -> Path:
        if ai_dir:
            base = Path(ai_dir) / "artifacts" / phase
        else:
            base = Path("/tmp/axiom-files/_unknown") / "artifacts" / phase
        return base

    def _make_summary(self, text: str) -> str:
        lines = text.splitlines()
        head = lines[: self._keep_head]
        tail = lines[-self._keep_tail :] if len(lines) > self._keep_tail else []
        preview = lines[: self._preview_lines]
        sections: list[str] = []
        sections.append(f"--- preview (first {min(len(lines), self._preview_lines)} of {len(lines)} lines) ---")
        sections.append("\n".join(preview))
        if head:
            sections.append(f"--- head ({len(head)} lines) ---")
            sections.append("\n".join(head))
        if tail:
            sections.append(f"--- tail ({len(tail)} lines) ---")
            sections.append("\n".join(tail))
        return "\n".join(sections)

    @staticmethod
    def _sanitize_argv_token(token: str) -> str:
        if not token:
            return ""
        if any(ch in token for ch in ("\n", "\r", "\x00")):
            return json_escape(token)
        if len(token) > 200:
            return token[:200] + "..."
        return token


def json_escape(token: str) -> str:
    return (
        token.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\x00", "\\x00")
    )


_DEFAULT: OutputWrangler | None = None


def default_wrangler() -> OutputWrangler:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = OutputWrangler()
    return _DEFAULT
