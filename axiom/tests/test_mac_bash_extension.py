from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
EXTENSION = (
    ROOT
    / "src/axiom/dispatcher/extensions/axiom-mac-bash/index.ts"
)


def test_mac_bash_extension_contract() -> None:
    source = EXTENSION.read_text(encoding="utf-8")
    schema = source.split("parameters: Type.Object({", 1)[1].split(
        "}),\n    async execute", 1
    )[0]

    assert 'name: "mac_bash"' in source
    assert "/api/v1/internal/mac-bridge/call" in source
    assert "AXIOM_SERVICE_TOKEN" in source
    assert 'op: "exec"' in source
    assert "timeout_ms: timeoutMs" in source
    assert "args," in source
    assert "device_id" in source  # returned in the result, not accepted as input
    assert "device_id" not in schema
    assert "max_output_bytes" not in source
    assert "command: Type.String" in schema
    assert "cwd: Type.Optional(Type.String" in schema
    assert "timeout_ms: Type.Optional(Type.Integer" in schema
    assert "minimum: MIN_TIMEOUT_MS" in source
    assert "maximum: MAX_TIMEOUT_MS" in source
    assert "DEFAULT_TIMEOUT_MS = 60_000" in source


def test_mac_bash_never_falls_back_to_a_local_shell_or_marks_nonzero_exit_as_error() -> None:
    source = EXTENSION.read_text(encoding="utf-8")

    assert "child_process" not in source
    assert "spawn(" not in source
    assert 'if (payload.result.ok === false)' in source
    # command/exit_code are returned through textResult; only transport,
    # ambiguity, device and protocol failures use the error path.
    assert "return textResult(renderResult(details), details);" in source


def test_mac_bash_extension_loads_through_the_pi_runtime() -> None:
    node = shutil.which("node")
    jiti = next(ROOT.glob("runtime/pi/*/node_modules/jiti"), None)
    if not node or jiti is None:
        pytest.skip("the bundled Pi runtime is required for extension loading tests")

    node_modules = jiti.parent
    script = f"""
const path = require('node:path');
const {{ createJiti }} = require({json.dumps(str(jiti))});
const jiti = createJiti(path.resolve('extension-probe.cjs'), {{ moduleCache: false }});
const extension = jiti({json.dumps(str(EXTENSION))});
const tools = [];
extension.default({{ registerTool(tool) {{ tools.push(tool); }}, on() {{}} }});
process.stdout.write(JSON.stringify({{
  names: tools.map(tool => tool.name),
  parameters: Object.keys(tools[0]?.parameters?.properties || {{}}).sort(),
}}));
"""
    result = subprocess.run(
        [node, "-e", script],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "NODE_PATH": str(node_modules)},
    )
    payload = json.loads(result.stdout)
    assert payload == {
        "names": ["mac_bash"],
        "parameters": ["command", "cwd", "timeout_ms"],
    }
