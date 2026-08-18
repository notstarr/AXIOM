import net from "node:net";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { errorResult, textResult } from "../../axiom_tools/runtime";

type BrokerResponse = {
  ok?: boolean;
  content?: string;
  data?: Record<string, unknown>;
  error?: { code?: string; message?: string; retryable?: boolean };
};

const MAX_BROKER_RESPONSE_BYTES = 2 * 1024 * 1024;
const LEGACY_BROWSER_COMMANDS = [
  "cdp_ctl.py",
  "launch-chrome.sh",
  "stop-chrome.sh",
  "launch-proxy.sh",
  "stop-proxy.sh",
  "proxy_get_traffic.py",
  "mitm_addon.py",
];

function bashCommand(input: unknown): string {
  if (!input || typeof input !== "object") return "";
  const value = input as Record<string, unknown>;
  return String(value.command ?? value.cmd ?? "");
}

const PAGE_ID = Type.String({ minLength: 1, description: "browser_open_page 返回的当前 Lease 页面 ID" });
const REF = Type.String({ minLength: 1, description: "browser_snapshot 返回的准确元素 Ref" });
const TIMEOUT = Type.Optional(Type.Integer({ minimum: 250, maximum: 60000, description: "本次操作超时毫秒数" }));

function brokerCall(operation: string, params: Record<string, unknown>, signal?: AbortSignal): Promise<BrokerResponse> {
  return new Promise((resolve, reject) => {
    const socketPath = process.env.AXIOM_BROWSER_SOCKET || "";
    const token = process.env.AXIOM_BROWSER_TOKEN || "";
    if (!socketPath || !token) {
      reject(new Error("browser_broker_unavailable: 当前阶段 Lease 没有 Browser Broker 身份"));
      return;
    }
    const socket = net.createConnection({ path: socketPath });
    let output = "";
    let settled = false;
    const finish = (error?: Error, response?: BrokerResponse) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
      socket.destroy();
      if (error) reject(error);
      else resolve(response || { ok: false, content: "Browser Broker 没有返回结果" });
    };
    const abort = () => finish(new Error("browser_operation_aborted: 浏览器操作已取消"));
    const timer = setTimeout(() => finish(new Error("browser_broker_timeout: Browser Broker 响应超时")), 65000);
    if (signal?.aborted) {
      abort();
      return;
    }
    signal?.addEventListener("abort", abort, { once: true });
    socket.setEncoding("utf8");
    socket.on("connect", () => {
      socket.write(`${JSON.stringify({ op: operation, token, params })}\n`);
    });
    socket.on("data", (chunk: string) => {
      output += chunk;
      if (Buffer.byteLength(output, "utf8") > MAX_BROKER_RESPONSE_BYTES) {
        finish(new Error("browser_broker_response_too_large: Browser Broker 响应超过上限"));
        return;
      }
      const newline = output.indexOf("\n");
      if (newline < 0) return;
      try {
        finish(undefined, JSON.parse(output.slice(0, newline)) as BrokerResponse);
      } catch (error) {
        finish(new Error(`browser_broker_invalid_response: ${String(error)}`));
      }
    });
    socket.on("error", (error) => finish(new Error(`browser_broker_unavailable: ${error.message}`)));
    socket.on("end", () => {
      if (!settled) finish(new Error("browser_broker_eof: Browser Broker 在返回结果前断开"));
    });
  });
}

function register(
  pi: ExtensionAPI,
  definition: {
    name: string;
    label: string;
    description: string;
    parameters: any;
  },
) {
  pi.registerTool({
    ...definition,
    promptSnippet: definition.description,
    async execute(_toolCallId, params, signal) {
      try {
        const response = await brokerCall(definition.name, params as Record<string, unknown>, signal);
        const details = { ...(response.data || {}), broker_error: response.error };
        if (!response.ok) {
          return errorResult(response.content || response.error?.message || "浏览器操作失败", details);
        }
        return textResult(response.content || "浏览器操作完成", details);
      } catch (error) {
        return errorResult(error instanceof Error ? error.message : String(error), { operation: definition.name });
      }
    },
  });
}

export default function axiomBrowser(pi: ExtensionAPI) {
  pi.on("tool_call", (event) => {
    if (event.toolName !== "bash") return;
    const command = bashCommand(event.input);
    const legacy = LEGACY_BROWSER_COMMANDS.find((name) => command.includes(name));
    if (!legacy) return;
    return {
      block: true,
      reason: `当前阶段已启用原生 browser_* Tool，禁止通过 Bash 调用旧浏览器脚本 ${legacy}`,
    };
  });

  register(pi, {
    name: "browser_open_page",
    label: "打开浏览器页面",
    description: "在当前阶段 Browser Lease 中创建独占页面并打开 URL，返回 page_id。",
    parameters: Type.Object({
      url: Type.String({ minLength: 1 }),
      wait_until: Type.Optional(Type.Union([
        Type.Literal("commit"), Type.Literal("domcontentloaded"), Type.Literal("load"), Type.Literal("networkidle"),
      ])),
      timeout_ms: TIMEOUT,
    }),
  });
  register(pi, {
    name: "browser_list_pages",
    label: "列出浏览器页面",
    description: "只列出当前阶段 Browser Lease 拥有的页面。",
    parameters: Type.Object({}),
  });
  register(pi, {
    name: "browser_close_page",
    label: "关闭浏览器页面",
    description: "关闭当前阶段 Browser Lease 的指定页面。",
    parameters: Type.Object({ page_id: PAGE_ID }),
  });
  register(pi, {
    name: "browser_navigate",
    label: "导航浏览器页面",
    description: "让指定页面导航到新的 Scope 内 URL。导航后旧 Ref 全部失效。",
    parameters: Type.Object({
      page_id: PAGE_ID,
      url: Type.String({ minLength: 1 }),
      wait_until: Type.Optional(Type.Union([
        Type.Literal("commit"), Type.Literal("domcontentloaded"), Type.Literal("load"), Type.Literal("networkidle"),
      ])),
      timeout_ms: TIMEOUT,
    }),
  });
  register(pi, {
    name: "browser_read_page",
    label: "读取页面文本",
    description: "读取页面渲染后的可见文本，不返回完整 DOM。",
    parameters: Type.Object({
      page_id: PAGE_ID,
      scope: Type.Optional(Type.Union([Type.Literal("viewport"), Type.Literal("full")])),
      max_chars: Type.Optional(Type.Integer({ minimum: 1000, maximum: 128000 })),
    }),
  });
  register(pi, {
    name: "browser_snapshot",
    label: "读取页面交互快照",
    description: "读取页面的可访问性交互结构和准确 Ref；交互前优先使用本工具。",
    parameters: Type.Object({
      page_id: PAGE_ID,
      scope: Type.Optional(Type.Union([
        Type.Literal("interactive"), Type.Literal("viewport"), Type.Literal("subtree"),
        Type.Literal("full"), Type.Literal("changes"),
      ])),
      ref: Type.Optional(REF),
    }),
  });
  register(pi, {
    name: "browser_click",
    label: "点击页面元素",
    description: "点击 Snapshot 中 allowed_actions 包含 click 的准确 Ref。",
    parameters: Type.Object({ page_id: PAGE_ID, ref: REF, timeout_ms: TIMEOUT }),
  });
  register(pi, {
    name: "browser_fill",
    label: "填写页面元素",
    description: "填写 Snapshot 中 allowed_actions 包含 fill 的准确 Ref。",
    parameters: Type.Object({ page_id: PAGE_ID, ref: REF, value: Type.String(), timeout_ms: TIMEOUT }),
  });
  register(pi, {
    name: "browser_press",
    label: "发送键盘按键",
    description: "向 Snapshot 中 allowed_actions 包含 press 的准确 Ref 发送按键。",
    parameters: Type.Object({ page_id: PAGE_ID, key: Type.String({ minLength: 1 }), ref: REF, timeout_ms: TIMEOUT }),
  });
  register(pi, {
    name: "browser_hover",
    label: "悬停页面元素",
    description: "悬停 Snapshot 中 allowed_actions 包含 hover 的准确 Ref。",
    parameters: Type.Object({ page_id: PAGE_ID, ref: REF, timeout_ms: TIMEOUT }),
  });
  register(pi, {
    name: "browser_scroll",
    label: "滚动页面",
    description: "将指定 Ref 滚动到视口，或按 delta 滚动当前页面。",
    parameters: Type.Object({
      page_id: PAGE_ID,
      ref: Type.Optional(REF),
      delta_x: Type.Optional(Type.Number()),
      delta_y: Type.Optional(Type.Number()),
      timeout_ms: TIMEOUT,
    }),
  });
  register(pi, {
    name: "browser_select",
    label: "选择下拉选项",
    description: "按 option value 或唯一可见标签选择下拉选项。",
    parameters: Type.Object({ page_id: PAGE_ID, ref: REF, value: Type.String(), timeout_ms: TIMEOUT }),
  });
  register(pi, {
    name: "browser_check",
    label: "切换勾选状态",
    description: "勾选或取消 Snapshot 中可 check 的控件。",
    parameters: Type.Object({ page_id: PAGE_ID, ref: REF, checked: Type.Optional(Type.Boolean()), timeout_ms: TIMEOUT }),
  });
  register(pi, {
    name: "browser_upload_file",
    label: "上传文件",
    description: "向文件输入 Ref 上传当前运行目录内的文件。",
    parameters: Type.Object({ page_id: PAGE_ID, ref: REF, file_paths: Type.Array(Type.String({ minLength: 1 }), { minItems: 1 }), timeout_ms: TIMEOUT }),
  });
  register(pi, {
    name: "browser_wait",
    label: "等待页面条件",
    description: "按文本、URL、加载状态、Ref、下载或必要的短时间等待页面。",
    parameters: Type.Object({
      page_id: PAGE_ID,
      condition: Type.Union([
        Type.Literal("text"), Type.Literal("url"), Type.Literal("load_state"),
        Type.Literal("ref"), Type.Literal("download"), Type.Literal("time"),
      ]),
      value: Type.Optional(Type.String()),
      state: Type.Optional(Type.Union([Type.Literal("domcontentloaded"), Type.Literal("load"), Type.Literal("networkidle")])),
      duration_ms: Type.Optional(Type.Integer({ minimum: 0, maximum: 30000 })),
      after_cursor: Type.Optional(Type.Integer({ minimum: 0 })),
      timeout_ms: TIMEOUT,
    }),
  });
  register(pi, {
    name: "browser_screenshot",
    label: "保存页面截图",
    description: "保存PNG/JPEG截图；labels=true 时标注当前 Snapshot Ref。",
    parameters: Type.Object({
      page_id: PAGE_ID,
      labels: Type.Optional(Type.Boolean()),
      full_page: Type.Optional(Type.Boolean()),
      output_path: Type.Optional(Type.String({ minLength: 1 })),
      quality: Type.Optional(Type.Integer({ minimum: 1, maximum: 100 })),
    }),
  });
  register(pi, {
    name: "browser_read_network",
    label: "读取页面网络活动",
    description: "按游标增量读取当前页面的HTTP和WebSocket活动，不会清空其他记录。",
    parameters: Type.Object({
      page_id: PAGE_ID,
      after_cursor: Type.Optional(Type.Integer({ minimum: 0 })),
      limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 500 })),
      url_contains: Type.Optional(Type.String()),
      include_sensitive: Type.Optional(Type.Boolean({ description: "返回Authorization/Cookie等敏感Header，需谨慎使用" })),
    }),
  });
  register(pi, {
    name: "browser_read_console",
    label: "读取页面Console",
    description: "按游标增量读取当前页面的Console、Page Error和Dialog。",
    parameters: Type.Object({
      page_id: PAGE_ID,
      after_cursor: Type.Optional(Type.Integer({ minimum: 0 })),
      limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 500 })),
    }),
  });
  register(pi, {
    name: "browser_evaluate",
    label: "执行聚焦页面检查",
    description: "执行聚焦的页面内JavaScript检查；不得替代正常读取、Snapshot和Ref交互。",
    parameters: Type.Object({
      page_id: PAGE_ID,
      expression: Type.String({ minLength: 1 }),
      argument: Type.Optional(Type.Any()),
      timeout_ms: TIMEOUT,
    }),
  });
  register(pi, {
    name: "browser_get_credentials",
    label: "提取凭证与Cookie",
    description: "从当前受控浏览器中提取指定主域名（如 '*.qq.com' 或 'qq.com'）或子域名的所有有效 Cookie（含 HttpOnly）、格式化 Header 与 Storage 凭证。",
    parameters: Type.Object({
      domain: Type.Optional(Type.String({ description: "目标主域名或通配符表达式（如 'qq.com'、'*.qq.com'、'mail.qq.com'）" })),
      page_id: Type.Optional(PAGE_ID),
      include_storage: Type.Optional(Type.Boolean({ description: "是否同时提取相关已打开页面的 localStorage 与 sessionStorage（默认 true）" })),
      include_expired: Type.Optional(Type.Boolean({ description: "是否包含已过期的 Cookie（默认 false）" })),
      timeout_ms: TIMEOUT,
    }),
  });
}
