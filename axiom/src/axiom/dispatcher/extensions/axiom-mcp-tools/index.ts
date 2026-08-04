import { spawn } from "node:child_process";
import fs from "node:fs";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { errorResult, textResult, boundedText, runDir } from "../../axiom_tools/runtime";

type SnapshotTool = {
  key: string;
  active_name: string;
  server_name: string;
  tool_name: string;
  title?: string;
  description?: string;
  input_schema?: Record<string, unknown>;
};

const MAX_OUTPUT = 128_000;

function readSnapshot(): SnapshotTool[] {
  const path = process.env.AXIOM_CAPABILITY_SNAPSHOT_PATH || "";
  if (!path) return [];
  try {
    const payload = JSON.parse(fs.readFileSync(path, "utf8"));
    return Array.isArray(payload?.mcp_tools) ? payload.mcp_tools : [];
  } catch (error) {
    throw new Error(`mcp_tool_load_failed: 无法读取 MCP 能力快照：${String(error)}`);
  }
}

function schema(value: Record<string, unknown> | undefined): any {
  const candidate = value && value.type === "object" ? value : { type: "object", properties: {}, additionalProperties: true };
  return Type.Unsafe(candidate as any);
}

function runMcpTool(tool: SnapshotTool, params: unknown, signal?: AbortSignal): Promise<{ code: number; stdout: string; stderr: string; timedOut: boolean }> {
  return new Promise((resolve) => {
    const python = process.env.AXIOM_PYTHON || "python3";
    const timeoutRaw = Number(process.env.AXIOM_MCP_TOOL_TIMEOUT_SECONDS || "120");
    const timeoutMs = Math.min(Math.max(Number.isFinite(timeoutRaw) ? timeoutRaw * 1000 : 120_000, 1_000), 600_000);
    const child = spawn(
      python,
      ["-m", "axiom.mcp_tool_runner", "--snapshot", process.env.AXIOM_CAPABILITY_SNAPSHOT_PATH || "", "--tool-id", tool.key],
      {
        cwd: runDir(),
        env: { ...process.env, PYTHONUNBUFFERED: "1" },
        stdio: ["pipe", "pipe", "pipe"],
        detached: false,
      },
    );
    let stdout = "";
    let stderr = "";
    let timedOut = false;
    const append = (current: string, chunk: Buffer) => boundedText(current + chunk.toString("utf8"), MAX_OUTPUT);
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
    child.on("close", (code) => {
      clearTimeout(timer);
      signal?.removeEventListener("abort", stop);
      resolve({ code: code ?? 1, stdout, stderr, timedOut });
    });
    child.stdin.end(JSON.stringify(params && typeof params === "object" ? params : {}));
  });
}

export default function axiomMcpTools(pi: ExtensionAPI) {
  const tools = readSnapshot();
  const names = new Set(tools.map((item) => item.active_name).filter(Boolean));
  if (tools.some((item) => !item.key || !item.active_name || !item.server_name || !item.tool_name)) {
    throw new Error("mcp_tool_load_failed: MCP 能力快照包含无效工具项");
  }
  if (names.size !== tools.length) {
    throw new Error("mcp_tool_load_failed: MCP 能力快照包含重复注册名");
  }
  for (const item of tools) {
    pi.registerTool({
      name: item.active_name,
      label: item.title || `${item.server_name}:${item.tool_name}`,
      description: item.description || `调用 MCP Server ${item.server_name} 的 ${item.tool_name}`,
      promptSnippet: `调用 ${item.server_name} 的 ${item.tool_name}`,
      parameters: schema(item.input_schema),
      async execute(_toolCallId, params, signal) {
        const result = await runMcpTool(item, params, signal);
        const details = {
          server: item.server_name,
          tool: item.tool_name,
          exit_code: result.code,
          timed_out: result.timedOut,
        };
        const output = boundedText(result.stdout || result.stderr || "(empty MCP result)", MAX_OUTPUT);
        if (result.code !== 0 || result.timedOut) return errorResult(JSON.stringify({ ...details, output }, null, 2), details);
        return textResult(output, details);
      },
    });
  }
  // Keep a reference so an empty snapshot is still a valid extension and so
  // accidental duplicate registrations can be diagnosed before Pi calls a
  // model.  ``--tools`` remains the final allowlist authority.
  void names;
}
