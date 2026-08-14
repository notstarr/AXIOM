#!/usr/bin/env python3
"""Validate the pinned Pi and Node bundle supplied to an AXIOM image build."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, NoReturn

PI_VERSION = "0.82.1"
NODE_VERSION = "v22.22.2"
PI_PACKAGE = "@earendil-works/pi-coding-agent"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REQUIRED_MANIFEST_FIELDS = (
    "os",
    "architecture",
    "pi_version",
    "node_version",
    "pi_package",
    "pi_entry",
    "pi_sha256",
    "node_sha256",
    "source",
    "built_at",
)


def fail(message: str) -> NoReturn:
    print(f"[axiom-runtime] ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def executable(path: Path, label: str) -> None:
    if not path.is_file():
        fail(f"{label} runtime is missing: {path}")
    if not os.access(path, os.X_OK):
        fail(f"{label} runtime is not executable: {path}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"cannot read {label}: {exc}")
    if not isinstance(payload, dict):
        fail(f"{label} must be a JSON object")
    return payload


def validate_manifest(manifest: Path) -> dict[str, Any]:
    if not manifest.is_file():
        fail(f"runtime manifest is missing: {manifest}")
    payload = read_json(manifest, "runtime manifest")
    missing = [field for field in REQUIRED_MANIFEST_FIELDS if not payload.get(field)]
    if missing:
        fail(f"runtime manifest is missing required fields: {', '.join(missing)}")
    if str(payload["os"]).strip().lower() != "linux":
        fail(f"runtime manifest os must be linux, got {payload['os']!r}")
    if str(payload["architecture"]).strip().lower() not in {"x86_64", "amd64"}:
        fail(
            "runtime manifest architecture must be x86_64 or amd64, "
            f"got {payload['architecture']!r}"
        )
    if payload["pi_version"] != PI_VERSION:
        fail(f"runtime manifest Pi version must be {PI_VERSION}, got {payload['pi_version']!r}")
    if payload["node_version"] != NODE_VERSION:
        fail(f"runtime manifest Node version must be {NODE_VERSION}, got {payload['node_version']!r}")
    if payload["pi_package"] != PI_PACKAGE:
        fail(f"runtime manifest Pi package must be {PI_PACKAGE}, got {payload['pi_package']!r}")
    for field in ("pi_sha256", "node_sha256"):
        value = str(payload[field]).lower()
        if not SHA256_RE.fullmatch(value):
            fail(f"runtime manifest {field} must be a lowercase SHA-256 digest")
        payload[field] = value
    for field in ("source", "built_at"):
        if not isinstance(payload[field], str) or not payload[field].strip():
            fail(f"runtime manifest {field} must be a non-empty string")
    try:
        datetime.fromisoformat(str(payload["built_at"]).replace("Z", "+00:00"))
    except ValueError as exc:
        fail(f"runtime manifest built_at must be ISO-8601: {exc}")
    if not isinstance(payload["pi_entry"], str) or not payload["pi_entry"].strip():
        fail("runtime manifest pi_entry must be a non-empty package-relative path")
    if Path(payload["pi_entry"]).is_absolute() or ".." in Path(payload["pi_entry"]).parts:
        fail("runtime manifest pi_entry must stay inside the Pi package")
    return payload


def main() -> None:
    root = Path(os.environ.get("AXIOM_RUNTIME_ROOT", "/opt/axiom/runtime")).resolve()
    pi = root / "pi" / PI_VERSION / "bin" / "pi"
    node = root / "node" / NODE_VERSION / "bin" / "node"
    package_root = root / "pi" / PI_VERSION / "node_modules" / "@earendil-works" / "pi-coding-agent"
    metadata = package_root / "package.json"
    manifest_payload = validate_manifest(root / "manifest.json")

    executable(pi, "Pi")
    executable(node, "Node")
    if not metadata.is_file():
        fail(f"Pi package metadata is missing: {metadata}")
    payload = read_json(metadata, "Pi package metadata")
    if payload.get("name") != PI_PACKAGE:
        fail(f"expected package {PI_PACKAGE}, got {payload.get('name')!r}")
    if payload.get("version") != PI_VERSION:
        fail(f"expected Pi version {PI_VERSION}, got {payload.get('version')!r}")
    bin_field = payload.get("bin")
    entry = bin_field.get("pi") if isinstance(bin_field, dict) else None
    if not isinstance(entry, str) or not entry.strip():
        fail("Pi package metadata does not contain bin.pi")
    if entry != manifest_payload["pi_entry"]:
        fail(
            "runtime manifest pi_entry does not match package bin.pi "
            f"({manifest_payload['pi_entry']!r} != {entry!r})"
        )
    entry_path = (metadata.parent / entry).resolve()
    try:
        entry_path.relative_to(metadata.parent.resolve())
    except ValueError:
        fail(f"Pi bin.pi escapes the package: {entry}")
    executable(entry_path, "Pi CLI entrypoint")

    try:
        node_version = subprocess.run(
            [str(node), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        fail(f"cannot execute Node: {exc}")
    if node_version != NODE_VERSION:
        fail(f"expected Node {NODE_VERSION}, got {node_version or 'unknown'}")

    pi_digest = sha256(pi)
    node_digest = sha256(node)
    if pi_digest != manifest_payload["pi_sha256"]:
        fail("Pi SHA-256 does not match runtime manifest")
    if node_digest != manifest_payload["node_sha256"]:
        fail("Node SHA-256 does not match runtime manifest")

    print(
        json.dumps(
            {
                "status": "ok",
                "runtime_root": str(root),
                "manifest": manifest_payload,
                "pi_version": PI_VERSION,
                "node_version": NODE_VERSION,
                "pi_sha256": pi_digest,
                "node_sha256": node_digest,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
