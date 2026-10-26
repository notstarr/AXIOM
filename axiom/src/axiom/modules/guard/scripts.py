"""Bounded read-only snapshots of the scripts a tool call will execute.

The guard judges one line of text: ``bash  python3 exp.py``.  Everything the
script actually does is invisible to it, which is not a gap a better regex can
close — the text simply is not there.  This module makes the content available.

Two decisions shape the implementation:

**The Worker reads the files, not the server.**  ``GuardRequest.cwd`` is a path in
whatever environment the Worker runs in.  Locally that is the same filesystem the
server sees, but inside a Cube sandbox it is a path in the container, so a server
that opened it would read a *different* file of the same name — worse than reading
nothing, because the judge would then be confidently wrong.  The guard extension
runs in the same process as the tool call, so it resolves paths correctly by
construction.

**Only statically resolvable references are captured.**  Following shell expansion
or executing anything to discover a path would be both unsafe and unreliable.  A
reference that cannot be resolved is reported as ``incomplete`` rather than
silently skipped, so the judge knows the script's body is missing instead of
inferring "no side effects" from an empty snapshot.

Known limitation: the redactor's shell-assignment rule (``_SHELL_ASSIGN`` in
``guard.redact``) treats an unquoted value as everything up to whitespace or
``;&|``, so a line of *code* such as ``s3.delete_object(Bucket="b", Key=x["K"])``
loses its tail when masked, leaving ``Key=[密钥]``.  This predates snapshots and
affects commands equally; it is recorded here rather than fixed because the rule
is shared with shell masking, where an unquoted value legitimately may contain
those characters.  It clips trailing text on a line, never the whole snapshot, so
the judge still sees the surrounding statements.
"""

from __future__ import annotations

from axiom.modules.guard.schemas import GuardScript, GuardScriptStatus


# Resolution limits.  These bound the whole capture, not each file: a command that
# references eight scripts still adds at most this much to the guard request, and
# the per-file cap keeps one huge file from crowding out the others.
MAX_SCRIPT_FILES = 8
MAX_SCRIPT_BYTES = 128 * 1024
MAX_TOTAL_SCRIPT_BYTES = 256 * 1024


def _truncate(content: str) -> tuple[str, bool]:
    encoded = content.encode("utf-8", errors="replace")
    if len(encoded) <= MAX_SCRIPT_BYTES:
        return content, False
    cut = encoded[:MAX_SCRIPT_BYTES].decode("utf-8", errors="ignore")
    return cut, True


def normalize_scripts(
    scripts: list[GuardScript] | None,
    status: GuardScriptStatus,
    *,
    max_total_bytes: int = MAX_TOTAL_SCRIPT_BYTES,
) -> tuple[list[GuardScript], GuardScriptStatus]:
    """Validate, bound and de-duplicate the snapshot the Worker supplied.

    The Worker is trusted to read the right files but not to bound them: a bug
    there — or a hostile tool payload that makes the extension read something huge
    — must not turn into an unbounded guard request.  Sizes sent by the client are
    ignored and recomputed here for the same reason the digest is: the only number
    the judge can rely on is one this process computed.
    """

    if status == "not_applicable" or not scripts:
        return [], status

    accepted: list[GuardScript] = []
    seen: set[tuple[str, str]] = set()
    total = 0
    truncated_any = False
    incomplete = status == "incomplete"
    for script in scripts[:MAX_SCRIPT_FILES]:
        path = (script.path or "").strip() or "<无法确定的脚本>"
        content = script.content or ""
        key = (path, content[:256])
        if key in seen:
            continue
        seen.add(key)
        content, cut_here = _truncate(content)
        if cut_here:
            incomplete = True
            truncated_any = True
        encoded_len = len(content.encode("utf-8", errors="replace"))
        if total + encoded_len > max_total_bytes:
            incomplete = True
            truncated_any = True
            continue
        total += encoded_len
        if script.read_error:
            incomplete = True
        accepted.append(
            script.model_copy(
                update={
                    "path": path,
                    "content": content,
                    "bytes": encoded_len,
                    "truncated": cut_here or script.truncated,
                    "complete": bool(script.complete) and not cut_here and not script.read_error,
                }
            )
        )
    if len(scripts) > MAX_SCRIPT_FILES:
        incomplete = True
    if not accepted and scripts:
        # Everything was rejected; say so instead of reporting a clean empty set.
        incomplete = True
    if truncated_any:
        incomplete = True
    return accepted, "incomplete" if incomplete else status
