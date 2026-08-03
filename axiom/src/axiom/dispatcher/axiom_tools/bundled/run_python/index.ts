import { spawn } from "node:child_process";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { boundedText, errorResult, runDir, textResult } from "../../runtime";

const Parameters = Type.Object(
  {
    code: Type.String({ description: "要执行的 Python 代码" }),
    args: Type.Optional(Type.Array(Type.String())),
    timeout_ms: Type.Optional(Type.Integer({ minimum: 100, maximum: 30000, default: 10000 })),
  },
  { additionalProperties: false },
);

function runPython(code: string, args: string[], timeoutMs: number, signal?: AbortSignal): Promise<{ code: number; stdout: string; stderr: string; timedOut: boolean }> {
  return new Promise((resolve) => {
    const python = process.env.AXIOM_PYTHON || "python3";
    const child = spawn(python, ["-c", code, ...args], {
      cwd: runDir(),
      env: { ...process.env, PYTHONUNBUFFERED: "1" },
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    let timedOut = false;
    const append = (current: string, chunk: Buffer) => boundedText(current + chunk.toString("utf8"), 128000);
    child.stdout.on("data", (chunk: Buffer) => { stdout = append(stdout, chunk); });
    child.stderr.on("data", (chunk: Buffer) => { stderr = append(stderr, chunk); });
    const stop = () => child.kill("SIGTERM");
    signal?.addEventListener("abort", stop, { once: true });
    const timer = setTimeout(() => { timedOut = true; stop(); }, timeoutMs);
    child.on("error", (error) => {
      clearTimeout(timer);
      signal?.removeEventListener("abort", stop);
      resolve({ code: 127, stdout, stderr: `${stderr}\n${error.message}`.trim(), timedOut });
    });
    child.on("close", (status) => {
      clearTimeout(timer);
      signal?.removeEventListener("abort", stop);
      resolve({ code: status ?? 1, stdout, stderr, timedOut });
    });
  });
}

export default function runPythonExtension(pi: ExtensionAPI) {
  pi.registerTool({
    name: "run_python",
    label: "运行 Python",
    description: "使用 AXIOM 固定 Python 解释器运行受限的短脚本。",
    promptSnippet: "用 AXIOM Python 运行短解析或计算脚本",
    promptGuidelines: ["使用 run_python 做小型解析或计算；不要安装依赖，不要把它当作无限制 Shell。"],
    parameters: Parameters,
    executionMode: "sequential",
    async execute(_toolCallId, params, signal) {
      const timeout = Math.min(Math.max(params.timeout_ms || 10000, 100), 30000);
      const result = await runPython(params.code, params.args || [], timeout, signal);
      const payload = JSON.stringify({
        exit_code: result.code,
        timed_out: result.timedOut,
        stdout: result.stdout,
        stderr: result.stderr,
      }, null, 2);
      if (result.code !== 0 || result.timedOut) {
        return errorResult(payload, { exit_code: result.code, timed_out: result.timedOut });
      }
      return textResult(payload, { exit_code: result.code, timed_out: false });
    },
  });
}
