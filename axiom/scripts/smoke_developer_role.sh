#!/usr/bin/env bash
#
# 冒烟测试：gateway developer→system 自动降级
#
# 复现线上那个故障 —— 网关只认 `system`、拒绝 OpenAI 的 `developer` 角色。
# 断言三件事：
#   1. 探测能分辨「拒绝 developer 但接受 system」与「两个都挂」
#   2. 结论正确落到 pi 的 models.json（compat.supportsDeveloperRole）
#   3. 真实 pi 二进制在该 models.json 下不再报 provider error
#
# 不需要真实 LLM：全程用本地假网关。
#
# 用法：scripts/smoke_developer_role.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PY="${REPO_ROOT}/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"
PI_BINARY="${REPO_ROOT}/runtime/pi/0.85.1/bin/pi"

echo "仓库:      $REPO_ROOT"
echo "Python:    $PY"
if [ -x "$PI_BINARY" ]; then
  echo "Pi 二进制: $PI_BINARY"
else
  echo "Pi 二进制: 未找到（$PI_BINARY）→ 端到端步骤将跳过"
fi
echo

PI_BINARY="$PI_BINARY" "$PY" - <<'PY'
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / "src"))

from axiom.dispatcher.pi import PiBootstrapExecutor
from axiom.modules.workers.pi_config import PiWorkerConfig, probe_developer_role
from axiom.modules.workers.schemas import Worker


FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


class Gateway:
    """A local OpenAI-compatible endpoint with a configurable role policy."""

    def __init__(self, *, developer: bool, system: bool) -> None:
        self.developer = developer
        self.system = system
        self.roles: list[str] = []
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # silence access logging
                pass

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                role = body["messages"][0]["role"]
                gateway.roles.append(role)
                accepted = gateway.developer if role == "developer" else gateway.system
                if not accepted:
                    raw = json.dumps(
                        {"message": "The request is invalid", "code": "400001"}
                    ).encode()
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                # Pi always streams, so answer with SSE rather than a JSON body.
                model = body.get("model", "auto")
                chunks = [
                    {
                        "id": "smoke",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"role": "assistant", "content": "pong"},
                                "finish_reason": None,
                            }
                        ],
                    },
                    {
                        "id": "smoke",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": model,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    },
                ]
                if isinstance(body.get("stream_options"), dict):
                    chunks[1]["usage"] = {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    }
                payload = (
                    "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
                    + "data: [DONE]\n\n"
                )
                raw = payload.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self._server.server_port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def models_payload(*, developer_role_supported: bool | None) -> dict:
    worker = Worker(
        name="smoke-worker",
        worker_type="pi",
        capabilities=["explore"],
        max_running=1,
        priority=0,
        config={
            "base_url": "https://gateway.example",
            "api_key": "key",
            "model": "auto",
            "thinking_enabled": True,
            "developer_role_supported": developer_role_supported,
        },
        disabled=False,
        created_at="2026-10-08T00:00:00Z",
        updated_at="2026-10-08T00:00:00Z",
    )
    payload = PiBootstrapExecutor(worker)._models_payload()
    assert payload is not None
    return payload


def compat_of(payload: dict):
    return payload["providers"]["axiom"]["models"][0].get("compat")


def run_pi(base_url: str, compat: dict | None) -> tuple[bool, str]:
    """Run the real Pi binary against the fake gateway. Returns (ok, detail)."""

    binary = os.environ.get("PI_BINARY", "")
    if not binary or not Path(binary).is_file():
        return True, "skipped (pi binary missing)"

    model = {
        "id": "auto",
        "name": "auto",
        "contextWindow": 300000,
        "reasoning": True,
    }
    if compat:
        model["compat"] = compat

    with tempfile.TemporaryDirectory(prefix="axiom-smoke-") as tmp:
        agent_dir = Path(tmp)
        (agent_dir / "models.json").write_text(
            json.dumps(
                {
                    "providers": {
                        "axiom": {
                            "baseUrl": base_url,
                            "api": "openai-completions",
                            "apiKey": "dummy",
                            "models": [model],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        env = {
            **os.environ,
            "PI_CODING_AGENT_DIR": str(agent_dir),
            "PI_BASE_URL": base_url,
            "PI_API_KEY": "dummy",
            "PI_PROVIDER_API": "openai-completions",
        }
        try:
            completed = subprocess.run(
                [
                    binary,
                    "--provider", "axiom",
                    "--model", "auto",
                    "--mode", "json",
                    "--print",
                    "--thinking", "high",
                    "--no-tools",
                    "say pong",
                ],
                capture_output=True,
                text=True,
                timeout=120,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return False, "pi timed out"
    output = completed.stdout + completed.stderr
    if '"stopReason":"error"' in output:
        message = ""
        for line in output.splitlines():
            if '"errorMessage"' in line:
                message = line[:200]
                break
        return False, f"provider error: {message}"
    return True, "no provider error"


# ---------------------------------------------------------------- 1. 只认 system
print("步骤 1-4: 网关拒绝 developer、接受 system")
gateway = Gateway(developer=False, system=True)
try:
    verdict = probe_developer_role(gateway.base_url, "key", "auto", "openai-completions")
    check("probe 判定为不支持 developer", verdict is False, f"verdict={verdict}")
    check("探测恰好发了 developer + system 两次", gateway.roles == ["developer", "system"], str(gateway.roles))

    compat = compat_of(models_payload(developer_role_supported=False))
    check("models.json 下发 supportsDeveloperRole=false", compat == {"supportsDeveloperRole": False}, str(compat))

    ok, detail = run_pi(gateway.base_url, compat)
    check("真实 pi 运行无 provider error", ok, detail)
finally:
    gateway.close()

# ------------------------------------------------------------- 2. 支持 developer
print("\n步骤 5-8: 网关接受 developer（能力不能白丢）")
gateway = Gateway(developer=True, system=True)
try:
    verdict = probe_developer_role(gateway.base_url, "key", "auto", "openai-completions")
    check("probe 判定为支持 developer", verdict is True, f"verdict={verdict}")
    check("支持时只发一次请求", gateway.roles == ["developer"], str(gateway.roles))

    compat = compat_of(models_payload(developer_role_supported=True))
    check("models.json 不含 compat（保持 developer）", compat is None, str(compat))

    ok, detail = run_pi(gateway.base_url, None)
    check("真实 pi 运行无 provider error", ok, detail)
finally:
    gateway.close()

# ------------------------------------------------------------------ 3. 两者都挂
print("\n步骤 9: 网关两个角色都拒（不得误判为角色问题）")
gateway = Gateway(developer=False, system=False)
try:
    verdict = probe_developer_role(gateway.base_url, "key", "auto", "openai-completions")
    check("probe 返回 None（结论不明，不改动配置）", verdict is None, f"verdict={verdict}")
finally:
    gateway.close()

# ---------------------------------------------------------------------- 汇总
print()
if FAILURES:
    print(f"冒烟测试失败：{len(FAILURES)} 项")
    for item in FAILURES:
        print(f"  - {item}")
    sys.exit(1)
print("冒烟测试全部通过 ✅")
PY
