#!/usr/bin/env python3
"""Smoke-test the Linux Pi runtime and AXIOM's built-in extensions.

The smoke test intentionally does not contact a model provider.  It only
proves that the pinned Pi executable can load JSON/RPC mode and compile/load
all extensions that a Runner may inject.  A provider error after ``agent_start``
is therefore not treated as a runtime-loading failure.
"""
from __future__ import annotations

import argparse
import json
import os
import selectors
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, NoReturn

PI_VERSION = "0.82.1"
EXTENSIONS = (
    "axiom-guard",
    "axiom-browser",
    "axiom-mcp-tools",
    "axiom-tool-activation",
)
FATAL_MARKERS = (
    "failed to load extension",
    "error loading extension",
    "cannot find module",
    "err_module_not_found",
    "syntaxerror",
    "extension_load_failed",
    "mcp_tool_load_failed",
    "axiom_tool_load_failed",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", default=os.environ.get("AXIOM_RUNTIME_ROOT", "/opt/axiom/runtime"))
    parser.add_argument(
        "--extension-root",
        default="/app/axiom/src/axiom/dispatcher/extensions",
        help="directory containing the four built-in Pi extensions",
    )
    parser.add_argument("--startup-timeout", type=float, default=10.0)
    return parser.parse_args()


def fail(message: str) -> NoReturn:
    print(f"[axiom-runtime-smoke] ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def terminate(process: subprocess.Popen[bytes], timeout: float = 3.0) -> tuple[bytes, bytes]:
    if process.poll() is None:
        process.terminate()
        try:
            return process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
    return process.communicate()


def _read_ready(
    selector: selectors.BaseSelector,
    output: dict[str, bytearray],
) -> None:
    """Drain all bytes currently available from selected process pipes.

    Do not use ``TextIOWrapper.readline`` here: one Pi process write can contain
    several JSON events, and TextIOWrapper may buffer the later lines while the
    underlying file descriptor is no longer readable. Draining the descriptor
    directly makes the smoke test deterministic for both real Pi and tiny test
    runtimes.
    """

    for key, _ in selector.select(timeout=0.25):
        stream = key.fileobj
        name = str(key.data)
        try:
            data = os.read(stream.fileno(), 65536)
        except BlockingIOError:
            continue
        except OSError:
            data = b""
        if data:
            output[name].extend(data)
            continue
        try:
            selector.unregister(stream)
        except (KeyError, ValueError):
            pass


def _decode(output: dict[str, bytearray]) -> tuple[str, str]:
    return (
        bytes(output["stdout"]).decode("utf-8", errors="replace"),
        bytes(output["stderr"]).decode("utf-8", errors="replace"),
    )


def _json_event_types(stdout: str) -> set[str]:
    seen: set[str] = set()
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and isinstance(event.get("type"), str):
            seen.add(event["type"])
    return seen

def check_fatal(output: str) -> None:
    lowered = output.lower()
    found = [marker for marker in FATAL_MARKERS if marker in lowered]
    if found:
        fail(f"Pi extension loading reported {', '.join(found)}:\n{output[-4000:]}")


def run_json(pi: Path, extensions: list[Path], env: dict[str, str], timeout: float) -> dict[str, Any]:
    command = [str(pi), "--mode", "json", "--no-session", "--offline", "--provider", "google", "--model", "gemini-2.5-flash", "--api-key", "axiom-runtime-smoke"]
    for extension in extensions:
        command.extend(("--extension", str(extension)))
    command.extend(("-p", "AXIOM runtime smoke test; reply with FINISH"))
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    os.set_blocking(process.stdout.fileno(), False)
    os.set_blocking(process.stderr.fileno(), False)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    output = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + timeout
    seen: set[str] = set()
    while time.monotonic() < deadline and {"session", "agent_start"} - seen:
        _read_ready(selector, output)
        stdout, _ = _decode(output)
        seen.update(_json_event_types(stdout))
        if process.poll() is not None:
            break
    tail_out, tail_err = terminate(process)
    output["stdout"].extend(tail_out)
    output["stderr"].extend(tail_err)
    stdout, stderr = _decode(output)
    combined = stdout + "\n" + stderr
    check_fatal(combined)
    seen.update(_json_event_types(stdout))
    if "session" not in seen or "agent_start" not in seen:
        fail(f"Pi JSON mode did not reach agent_start (returncode={process.returncode}):\n{combined[-4000:]}")
    return {"mode": "json", "events": sorted(seen), "returncode": process.returncode}

def run_rpc(pi: Path, extensions: list[Path], env: dict[str, str], timeout: float) -> dict[str, Any]:
    command = [str(pi), "--mode", "rpc", "--no-session", "--offline"]
    for extension in extensions:
        command.extend(("--extension", str(extension)))
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    os.set_blocking(process.stdout.fileno(), False)
    os.set_blocking(process.stderr.fileno(), False)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    output = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + min(timeout, 5.0)
    while time.monotonic() < deadline:
        _read_ready(selector, output)
        if process.poll() is not None:
            break
    if process.poll() is not None and process.returncode not in (0, None):
        tail_out, tail_err = terminate(process)
        output["stdout"].extend(tail_out)
        output["stderr"].extend(tail_err)
        stdout, stderr = _decode(output)
        fail(
            f"Pi RPC mode exited during startup (returncode={process.returncode}):\n"
            f"{(stdout + stderr)[-4000:]}"
        )
    tail_out, tail_err = terminate(process)
    output["stdout"].extend(tail_out)
    output["stderr"].extend(tail_err)
    stdout, stderr = _decode(output)
    combined = stdout + "\n" + stderr
    check_fatal(combined)
    return {"mode": "rpc", "returncode": process.returncode, "started": True}

def main() -> None:
    args = parse_args()
    root = Path(args.runtime_root).resolve()
    pi = root / "pi" / PI_VERSION / "bin" / "pi"
    if not pi.is_file() or not os.access(pi, os.X_OK):
        fail(f"Pi executable is missing or not executable: {pi}")
    extension_root = Path(args.extension_root).resolve()
    extensions = []
    for name in EXTENSIONS:
        path = extension_root / name / "index.ts"
        if not path.is_file():
            fail(f"built-in extension is missing: {path}")
        extensions.append(path)

    with tempfile.TemporaryDirectory(prefix="axiom-pi-smoke-") as temp_dir:
        temp = Path(temp_dir)
        agent_dir = temp / "pi-agent"
        run_dir = temp / "run"
        agent_dir.mkdir()
        run_dir.mkdir()
        capability_snapshot = run_dir / "capabilities.json"
        capability_snapshot.write_text('{"mcp_tools": []}\n', encoding="utf-8")
        tool_snapshot = run_dir / "axiom-tools.json"
        tool_snapshot.write_text('{"tools": []}\n', encoding="utf-8")
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(temp),
                "PI_CODING_AGENT_DIR": str(agent_dir),
                "AXIOM_RUN_DIR": str(run_dir),
                "AXIOM_CAPABILITY_SNAPSHOT_PATH": str(capability_snapshot),
                "AXIOM_TOOL_SNAPSHOT_PATH": str(tool_snapshot),
                "AXIOM_TOOL_KEYS": "[]",
                "AXIOM_GUARD_ENABLED": "0",
                "AXIOM_PYTHON": sys.executable,
            }
        )
        results = [run_json(pi, extensions, env, args.startup_timeout), run_rpc(pi, extensions, env, args.startup_timeout)]

    print(json.dumps({"status": "ok", "results": results}, ensure_ascii=False))


if __name__ == "__main__":
    main()
