import fs from "node:fs";
import type { ExtensionAPI, ToolResultEvent } from "@earendil-works/pi-coding-agent";

type SnapshotTool = {
  key: string;
  native_name: string;
  server_name: string;
  tool_name: string;
  input_schema?: Record<string, unknown>;
};

type SnapshotServer = {
  id: string;
  name: string;
  native_name?: string;
  transport?: string;
  config: Record<string, unknown>;
  tool_exposure?: Record<string, string>;
};

type Snapshot = {
  schema_version?: number;
  mcp_mode?: string;
  mcp_tools?: SnapshotTool[];
  mcp_servers?: SnapshotServer[];
};

const MAX_OUTPUT = 128_000;
const RESOURCE_TOOLS = new Set([
  "list_mcp_resources",
  "list_mcp_resource_templates",
  "read_mcp_resource",
]);

function boundedText(value: string): string {
  if (value.length <= MAX_OUTPUT) return value;
  return `${value.slice(0, MAX_OUTPUT)}\n...[AXIOM output truncated]`;
}

function readSnapshot(): Snapshot {
  const path = process.env.AXIOM_CAPABILITY_SNAPSHOT_PATH || "";
  if (!path) throw new Error("axiom_mcp_load_failed: capability snapshot path is missing");
  let payload: unknown;
  try {
    payload = JSON.parse(fs.readFileSync(path, "utf8"));
  } catch (error) {
    throw new Error(`axiom_mcp_load_failed: cannot read capability snapshot: ${String(error)}`);
  }
  if (!payload || typeof payload !== "object") {
    throw new Error("axiom_mcp_load_failed: capability snapshot is not an object");
  }
  const snapshot = payload as Snapshot;
  if (snapshot.mcp_mode !== "pi_native" || snapshot.schema_version !== 3) {
    throw new Error("axiom_mcp_load_failed: snapshot is not a Pi native MCP snapshot");
  }
  if (!Array.isArray(snapshot.mcp_tools) || !Array.isArray(snapshot.mcp_servers)) {
    throw new Error("axiom_mcp_load_failed: snapshot is missing MCP tools or servers");
  }
  return snapshot;
}

function safeConfig(server: SnapshotServer): Record<string, unknown> {
  const config = { ...server.config };
  const serialized = JSON.stringify(config);
  if (serialized.includes("!command")) {
    throw new Error(`axiom_mcp_load_failed: command interpolation is forbidden for ${server.name}`);
  }
  if (/\$\{[^}]+\}/.test(serialized)) {
    throw new Error(`axiom_mcp_load_failed: unresolved environment variable in ${server.name}`);
  }
  const transport = String(server.transport || config.type || "").toLowerCase();
  if (transport === "sse" || String(config.url || "").endsWith("/sse")) {
    throw new Error(`axiom_mcp_load_failed: legacy SSE is not supported: ${server.name}`);
  }
  delete config.transport;
  config.exposure = "hidden";
  config.timeout = 120;
  config.enabled = true;
  const exposure = server.tool_exposure || {};
  config.toolExposure = exposure;
  return config;
}

function nativeToolNames(tools: SnapshotTool[]): Set<string> {
  return new Set(tools.map((item) => item.native_name).filter(Boolean));
}

function stable(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stable).join(",")}]`;
  if (!value || typeof value !== "object") return JSON.stringify(value);
  return `{${Object.keys(value as Record<string, unknown>).sort().map((key) => `${JSON.stringify(key)}:${stable((value as Record<string, unknown>)[key])}`).join(",")}}`;
}

function assertNoLeakedTools(pi: ExtensionAPI, allowed: Set<string>): void {
  for (const tool of pi.getAllTools()) {
    if (!tool.name.startsWith("mcp__") || RESOURCE_TOOLS.has(tool.name)) continue;
    if (!allowed.has(tool.name) && tool.exposure !== "hidden") {
      throw new Error(`axiom_mcp_load_failed: unauthorized MCP tool is exposed: ${tool.name}`);
    }
  }
}

function assertSnapshotSchemas(
  pi: ExtensionAPI,
  tools: SnapshotTool[],
  requirePresent = false,
): void {
  const all = pi.getAllTools();
  for (const item of tools) {
    const actual = all.find((tool) => tool.name === item.native_name);
    if (!actual) {
      if (requirePresent) {
        throw new Error(`axiom_mcp_load_failed: MCP tool was not registered: ${item.native_name}`);
      }
      continue;
    }
    if (!item.input_schema) continue;
    if (stable(actual.parameters) !== stable(item.input_schema)) {
      throw new Error(`axiom_mcp_load_failed: MCP schema drift detected: ${item.native_name}`);
    }
  }
}

export default function axiomNativeMcp(pi: ExtensionAPI) {
  const snapshot = readSnapshot();
  const tools = snapshot.mcp_tools || [];
  const servers = snapshot.mcp_servers || [];
  const allowed = nativeToolNames(tools);
  const byServer = new Map<string, SnapshotTool[]>();
  for (const tool of tools) {
    if (!tool.key || !tool.native_name || !tool.server_name || !tool.tool_name) {
      throw new Error("axiom_mcp_load_failed: invalid MCP tool entry");
    }
    const list = byServer.get(tool.server_name) || [];
    list.push(tool);
    byServer.set(tool.server_name, list);
  }
  for (const server of servers) {
    const selected = byServer.get(server.name) || [];
    if (!selected.length) continue;
    const config = safeConfig(server);
    config.toolExposure = Object.fromEntries(selected.map((item) => [item.tool_name, "direct"]));
    pi.registerMcpServer(server.native_name || server.name, config as never);
  }

  pi.on("session_start", () => {
    assertNoLeakedTools(pi, allowed);
    assertSnapshotSchemas(pi, tools);
  });

  pi.on("mcp_servers_change", () => {
    assertNoLeakedTools(pi, allowed);
    // The built-in MCP extension emits this while a newly registered server
    // is still connecting.  Do not fail on the transient missing tools; the
    // strict check below runs immediately before the first model turn.
    assertSnapshotSchemas(pi, tools);
  });

  pi.on("before_agent_start", () => {
    assertNoLeakedTools(pi, allowed);
    assertSnapshotSchemas(pi, tools, true);
  });

  pi.on("tool_call", (event) => {
    if (!event.toolName.startsWith("mcp__")) return;
    if (!allowed.has(event.toolName)) {
      return { block: true, reason: "AXIOM Lease 未授权该 MCP Tool" };
    }
  });

  pi.on("tool_result", (event: ToolResultEvent) => {
    if (!event.toolName.startsWith("mcp__")) return;
    const content = event.content.map((item) => {
      if (item.type !== "text") return item;
      return { ...item, text: boundedText(item.text) };
    });
    return {
      content,
      isError: event.isError,
      ...(event.structuredContent === undefined
        ? {}
        : { structuredContent: event.structuredContent }),
    };
  });
}
