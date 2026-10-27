"""Which raw run-directory outputs are worth keeping.

Two very different paths write a Worker's raw files to durable storage, and they
must agree on what counts as a keeper:

* the conclude path **registers** selected files as Artifacts (``explore.work``);
* the sandbox cleanup path **rescues** whatever is left before the run directory
  is deleted.

They used to disagree badly.  Registration was bounded — 40 files, 10 MiB each,
32 MiB total — while the rescue moved ``work/``, ``artifacts/`` and ``evidence/``
wholesale.  A Worker could therefore write a 400 MiB bulk download that the
collector refused to register and the rescue copied anyway, unregistered and
invisible, for every lease of the Task.  That is how one Task accumulated 2 GiB
of preserved raw files against 238 MiB of actual Artifacts.

The rule this module encodes is the operator's: **``work/`` holds key source data
and scripts, nothing else**.  Anything outside that is a bulk download that
belongs in evidence (if it is reported) or nowhere (if it is not).
"""

from __future__ import annotations

# Source data and scripts.  Deliberately an allowlist: a denylist would keep
# whatever new binary a Worker happened to dump.
KEY_OUTPUT_SUFFIXES = frozenset(
    {
        # scripts
        ".py",
        ".sh",
        ".bash",
        ".js",
        ".mjs",
        ".cjs",
        ".ts",
        ".rb",
        ".pl",
        ".go",
        ".php",
        ".ps1",
        # source data / samples
        ".json",
        ".jsonl",
        ".ndjson",
        ".csv",
        ".tsv",
        ".txt",
        ".log",
        ".md",
        ".yaml",
        ".yml",
        ".xml",
        ".html",
        ".htm",
        ".sql",
        ".conf",
        ".ini",
        ".env",
        ".pem",
        ".patch",
        ".diff",
    }
)

# Directories that are never worth keeping: caches, vendored trees, VCS metadata.
SKIP_DIRECTORIES = frozenset(
    {
        "__pycache__",
        ".git",
        ".svn",
        ".hg",
        ".cache",
        ".npm",
        ".cargo",
        ".rustup",
        ".venv",
        "venv",
        "node_modules",
        "site-packages",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
    }
)

# Bounds shared with the conclude collector.  A file above ``MAX_FILE_BYTES``, or
# a directory above ``MAX_TOTAL_BYTES``, is a bulk download rather than a source
# sample: report it through evidence or not at all.
MAX_FILES = 40
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024


def is_key_output(name: str) -> bool:
    """Return whether ``name`` may be preserved as a raw record.

    Suffix based, because the size check needs a stat that the caller does
    separately (and can fail).
    """

    lowered = name.lower()
    dot = lowered.rfind(".")
    if dot <= 0:
        # No suffix.  Workers do write bare scripts (``probe``, ``scan``); keep
        # them only when they are small, which the caller enforces by size.
        return True
    return lowered[dot:] in KEY_OUTPUT_SUFFIXES


def select_preservable(root, *, relative_to=None) -> list:
    """Pick the files under ``root`` worth rescuing, respecting every bound.

    Returns paths in a stable order so repeated cleanups behave identically.
    The caller owns the move and any logging.
    """

    from pathlib import Path

    root = Path(root)
    if not root.is_dir() or root.is_symlink():
        return []
    base = Path(relative_to) if relative_to is not None else root
    kept: list[Path] = []
    total = 0
    for current in sorted(p for p in root.rglob("*") if p.is_file()):
        if any(part in SKIP_DIRECTORIES for part in current.relative_to(base).parts):
            continue
        if current.is_symlink() or not is_key_output(current.name):
            continue
        try:
            size = current.stat().st_size
        except OSError:
            continue
        if size > MAX_FILE_BYTES:
            continue
        if len(kept) >= MAX_FILES or total + size > MAX_TOTAL_BYTES:
            break
        kept.append(current)
        total += size
    return kept
