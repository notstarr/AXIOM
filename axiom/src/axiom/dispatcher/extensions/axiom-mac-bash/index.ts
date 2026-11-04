import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { apiBase, boundedText, errorResult, textResult } from "../../axiom_tools/runtime";

const DEFAULT_TIMEOUT_MS = 60_000;
const MIN_TIMEOUT_MS = 1_000;
const MAX_TIMEOUT_MS = 600_000;
const GATEWAY_GRACE_MS = 15_000;

type MacBashInput = {
  command: string;
  cwd?: string;
  timeout_ms?: number;
};

type ExecResult = {
  ok?: boolean;
  stdout?: unknown;
  stderr?: unknown;
  exit_code?: unknown;
  duration_ms?: unknown;
  truncated_bytes?: unknown;
};

type BridgeResponse = {
  ok?: boolean;
  device_id?: string;
  result?: ExecResult;
  code?: string;
  error?: string;
  detail?: string;
};

type ExecDetails = {
  device_id: string;
  stdout: string;
  stderr: string;
  exit_code: number;
  duration_ms: number;
  truncated_bytes: number;
};

function integer(value: unknown): number {
  const number = Number(value ?? 0);
  return Number.isFinite(number) ? Math.trunc(number) : 0;
}

function renderResult(result: ExecDetails): string {
  return [
    `device_id: ${result.device_id}`,
    `exit_code: ${result.exit_code}`,
    `duration_ms: ${result.duration_ms}`,
    `truncated_bytes: ${result.truncated_bytes}`,
    "--- stdout ---",
    result.stdout || "(empty)",
    "--- stderr ---",
    result.stderr || "(empty)",
  ].join("\n");
}

async function executeMacBash(
  input: MacBashInput,
  signal?: AbortSignal,
): Promise<ReturnType<typeof textResult> | ReturnType<typeof errorResult>> {
  const command = typeof input.command === "string" ? input.command : "";
  if (!command.trim()) {
    return errorResult("command 不能为空", { code: "E_INVALID_ARGUMENT" });
  }

  const timeoutMs = input.timeout_ms ?? DEFAULT_TIMEOUT_MS;
  if (
    !Number.isInteger(timeoutMs)
    || timeoutMs < MIN_TIMEOUT_MS
    || timeoutMs > MAX_TIMEOUT_MS
  ) {
    return errorResult(
      `timeout_ms 必须是 ${MIN_TIMEOUT_MS}..${MAX_TIMEOUT_MS} 之间的整数`,
      { code: "E_INVALID_ARGUMENT" },
    );
  }

  const token = (process.env.AXIOM_SERVICE_TOKEN || "").trim();
  if (!token) {
    return errorResult("当前 Worker 没有 AXIOM service token", { code: "E_AUTH" });
  }

  const args: Record<string, unknown> = {
    command,
    timeout_ms: timeoutMs,
  };
  const cwd = typeof input.cwd === "string" ? input.cwd.trim() : "";
  if (cwd) args.cwd = cwd;

  const controller = new AbortController();
  let gatewayTimedOut = false;
  const abortFromPi = () => controller.abort();
  if (signal?.aborted) {
    return errorResult("mac_bash 调用已取消", { code: "E_ABORTED" });
  }
  signal?.addEventListener("abort", abortFromPi, { once: true });
  const timer = setTimeout(() => {
    gatewayTimedOut = true;
    controller.abort();
  }, timeoutMs + GATEWAY_GRACE_MS);

  try {
    const response = await fetch(`${apiBase()}/api/v1/internal/mac-bridge/call`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({
        op: "exec",
        args,
        timeout_ms: timeoutMs,
      }),
      signal: controller.signal,
    });

    const raw = await response.text();
    let payload: BridgeResponse;
    try {
      payload = JSON.parse(raw) as BridgeResponse;
    } catch {
      return errorResult("Mac Bridge 返回了无法解析的响应", {
        code: "E_BAD_FRAME",
        http_status: response.status,
      });
    }

    if (!response.ok) {
      const code = response.status === 403 ? "E_AUTH" : (payload.code || "E_GATEWAY");
      const message = payload.error || payload.detail || `网关 HTTP ${response.status}`;
      return errorResult(`${code}：${message}`, {
        code,
        http_status: response.status,
      });
    }
    if (!payload || typeof payload !== "object") {
      return errorResult("Mac Bridge 返回了空响应", { code: "E_BAD_FRAME" });
    }
    if (!payload.ok) {
      const code = payload.code || "E_DEVICE";
      return errorResult(`${code}：${payload.error || "Mac Bridge 调用失败"}`, {
        code,
        device_id: payload.device_id || "",
      });
    }
    if (!payload.result || typeof payload.result !== "object") {
      return errorResult("Mac Bridge 成功响应缺少执行结果", {
        code: "E_BAD_FRAME",
        device_id: payload.device_id || "",
      });
    }

    const details: ExecDetails = {
      device_id: payload.device_id || "",
      stdout: boundedText(payload.result.stdout),
      stderr: boundedText(payload.result.stderr),
      exit_code: integer(payload.result.exit_code),
      duration_ms: integer(payload.result.duration_ms),
      truncated_bytes: integer(payload.result.truncated_bytes),
    };
    if (payload.result.ok === false) {
      return errorResult(details.stderr || "Mac Bridge 执行失败", {
        ...details,
        code: "E_EXEC_FAILED",
      });
    }
    return textResult(renderResult(details), details);
  } catch (error) {
    if (signal?.aborted) {
      return errorResult("mac_bash 调用已取消", { code: "E_ABORTED" });
    }
    if (gatewayTimedOut) {
      return errorResult(`Mac Bridge 网关等待超过 ${timeoutMs + GATEWAY_GRACE_MS}ms`, {
        code: "E_GATEWAY_TIMEOUT",
        timeout_ms: timeoutMs,
      });
    }
    return errorResult(
      `Mac Bridge 请求失败：${error instanceof Error ? error.message : String(error)}`,
      { code: "E_REQUEST_FAILED" },
    );
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", abortFromPi);
  }
}

export default function axiomMacBash(pi: ExtensionAPI) {
  pi.registerTool({
    name: "mac_bash",
    label: "Mac 本机 Shell",
    description:
      "通过 Mac Bridge 在唯一在线的 Mac 上执行一条命令。每条命令都是独立的 "
      + "/bin/zsh -lc 进程，不保留上一条命令的 cd；省略 cwd 时使用 Mac 用户 Home。",
    promptSnippet: "在客户端 Mac 上执行本机 shell 命令，读取 .app、Mach-O、签名和 plist 等真实输出。",
    parameters: Type.Object({
      command: Type.String({
        minLength: 1,
        description: "要交给 Mac 的 /bin/zsh -lc 执行的完整命令",
      }),
      cwd: Type.Optional(Type.String({
        description: "命令工作目录；省略时使用 Mac 用户 Home",
      })),
      timeout_ms: Type.Optional(Type.Integer({
        minimum: MIN_TIMEOUT_MS,
        maximum: MAX_TIMEOUT_MS,
        default: DEFAULT_TIMEOUT_MS,
        description: "命令超时毫秒数；网关会在该时间后额外留出收尾余量",
      })),
    }),
    async execute(_toolCallId, params, signal) {
      return executeMacBash(params as MacBashInput, signal);
    },
  });
}
