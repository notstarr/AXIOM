import fs from "node:fs";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { reportToolEvent, registerToolLifecycleAudit } from "../../axiom_tools/runtime";

type SnapshotTool = {
  key: string;
  input_schema?: Record<string, unknown>;
};

function selectedKeys(): string[] {
  try {
    const value = JSON.parse(process.env.AXIOM_TOOL_KEYS || "[]");
    return Array.isArray(value) ? value.map((item) => String(item)).filter(Boolean) : [];
  } catch {
    return [];
  }
}

function snapshot(): SnapshotTool[] {
  const path = process.env.AXIOM_TOOL_SNAPSHOT_PATH || "";
  if (!path) return [];
  try {
    const payload = JSON.parse(fs.readFileSync(path, "utf8"));
    return Array.isArray(payload?.tools) ? payload.tools : [];
  } catch (error) {
    throw new Error(`axiom_tool_load_failed: 无法读取工具快照：${String(error)}`);
  }
}

function schemaContract(value: Record<string, unknown> | undefined): string {
  if (!value || value.type !== "object") return "invalid";
  const properties = value.properties && typeof value.properties === "object"
    ? Object.keys(value.properties as Record<string, unknown>).sort()
    : [];
  const required = Array.isArray(value.required)
    ? value.required.map((item) => String(item)).sort()
    : [];
  return JSON.stringify({ type: "object", properties, required });
}

export default function axiomToolActivation(pi: ExtensionAPI) {
  const keys = new Set(selectedKeys());
  let validated = false;
  let validationError = false;
  registerToolLifecycleAudit(pi, keys);
  const failLoad = async (error: unknown): Promise<never> => {
    const message = error instanceof Error ? error.message : String(error);
    validationError = true;
    await reportToolEvent({
      tool_key: keys.values().next().value || "<activation>",
      event_kind: "load_error",
      outcome: "load_error",
      error: message,
    });
    // Pi treats extension event exceptions as diagnostics and may continue
    // in print/JSON mode.  A mismatched AXIOM Tool contract is not recoverable
    // by the model, so make it a process-level, clearly classifiable failure
    // before another provider request is attempted.
    process.stderr.write(`${message}\n`);
    process.exit(78);
  };

  const validateAndActivate = async () => {
    if (!keys.size || validated) return;
    const expected = new Map(snapshot().map((item) => [item.key, item]));
    const all = new Map(pi.getAllTools().map((item) => [item.name, item]));
    for (const key of keys) {
      const tool = all.get(key);
      const snapshotItem = expected.get(key);
      if (!tool || !snapshotItem) {
        throw new Error(`axiom_tool_load_failed: 未注册选定工具 ${key}`);
      }
      if (
        snapshotItem.input_schema
        && schemaContract(tool.parameters as Record<string, unknown>) !== schemaContract(snapshotItem.input_schema)
      ) {
        throw new Error(`axiom_tool_load_failed: 工具 ${key} 的参数 Schema 与快照不一致`);
      }
    }
    const active = new Set([...pi.getActiveTools(), ...keys]);
    pi.setActiveTools([...active]);
    const after = new Set(pi.getActiveTools());
    for (const key of keys) {
      if (!after.has(key)) {
        throw new Error(`axiom_tool_load_failed: 工具 ${key} 无法激活`);
      }
    }
    validated = true;
  };

  pi.on("session_start", async () => {
    if (!keys.size || validationError) return;
    try {
      await validateAndActivate();
    } catch (error) {
      await failLoad(error);
    }
  });

  // Some Pi modes report a session_start handler exception as a diagnostic
  // and continue.  Re-check at the last pre-model hook so a stale/missing
  // registration can never silently turn into a normal model run.
  pi.on("before_agent_start", async () => {
    if (!keys.size || validated || validationError) return;
    try {
      await validateAndActivate();
    } catch (error) {
      await failLoad(error);
    }
  });
}
