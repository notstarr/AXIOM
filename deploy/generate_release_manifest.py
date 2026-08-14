#!/usr/bin/env python3
"""Write a machine-readable manifest for one immutable AXIOM image build."""
from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
from pathlib import Path
from typing import Any


def command_version(command: list[str]) -> str:
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"command {' '.join(command)!r} could not be executed: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        message = detail[0].strip() if detail else f"exit={result.returncode}"
        raise RuntimeError(f"command {' '.join(command)!r} failed: {message}")
    output = (result.stdout or result.stderr).strip().splitlines()
    if not output:
        raise RuntimeError(f"command {' '.join(command)!r} returned no version output")
    return output[0].strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    runtime = json.loads(args.runtime_manifest.read_text(encoding="utf-8"))
    rollback_raw = os.environ.get("AXIOM_DATABASE_ROLLBACK_COMPATIBLE", "0").strip().lower()
    rollback_values = {"0": False, "1": True, "false": False, "true": True, "no": False, "yes": True}
    if rollback_raw not in rollback_values:
        raise SystemExit(
            "AXIOM_DATABASE_ROLLBACK_COMPATIBLE must be one of 0, 1, false, true, no or yes"
        )
    tools: dict[str, list[str]] = {
        "python": ["python", "--version"],
        "curl": ["curl", "--version"],
        "jq": ["jq", "--version"],
        "nmap": ["nmap", "--version"],
        "ffuf": ["ffuf", "-V"],
        "katana": ["katana", "-version"],
        "httpx": ["httpx", "-version"],
        "subfinder": ["subfinder", "-version"],
        "nuclei": ["nuclei", "-version"],
        "sqlmap": ["sqlmap", "--version"],
        "chromium": [os.environ.get("CHROME_BIN", "/usr/bin/chromium"), "--version"],
    }
    payload: dict[str, Any] = {
        "axiom_version": os.environ.get("AXIOM_BUILD_VERSION", "unknown"),
        "git_revision": os.environ.get("AXIOM_BUILD_REVISION", "unknown"),
        "built_at": os.environ.get("AXIOM_BUILD_DATE", "unknown"),
        "database": {
            "migration_target": os.environ.get(
                "AXIOM_DATABASE_MIGRATION_TARGET", "postgresql@head"
            ),
            "rollback_compatible": rollback_values[rollback_raw],
        },
        "os": platform.system().lower(),
        "architecture": platform.machine().lower(),
        "runtime": runtime,
        "tools": {name: command_version(command) for name, command in tools.items()},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "ok", "manifest": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
