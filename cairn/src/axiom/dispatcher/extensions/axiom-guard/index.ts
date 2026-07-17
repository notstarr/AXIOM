import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";

type GuardDecision = {
  decision?: "ALLOW" | "BLOCK";
  reason?: string;
  event_id?: string;
  observed_decision?: "ALLOW" | "BLOCK";
  observed_reason?: string;
  source?: string;
};

function truthy(value: string | undefined): boolean {
  return ["1", "true", "yes", "on", "enabled"].includes((value ?? "").trim().toLowerCase());
}

function guardMode(): string | undefined {
  const raw = (process.env.AXIOM_GUARD_MODE || "").trim().toLowerCase();
  return ["off", "observe", "guarded", "enforce"].includes(raw) ? raw : undefined;
}

function baseUrl(): string {
  return (process.env.AXIOM_GUARD_SERVER_URL || process.env.AXIOM_SERVER_URL || "http://127.0.0.1:8888").replace(/\/$/, "");
}

function timeoutMs(): number {
  const parsed = Number(process.env.AXIOM_GUARD_TIMEOUT_MS || "3000");
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 3000;
}

async function postDecision(payload: Record<string, unknown>): Promise<GuardDecision | undefined> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs());
  try {
    const response = await fetch(`${baseUrl()}/tool-hooks/pre`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });
    if (!response.ok) {
      console.error(`AXIOM guard fail-open: backend returned ${response.status}`);
      return undefined;
    }
    return await response.json() as GuardDecision;
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    console.error(`AXIOM guard fail-open: ${message}`);
    return undefined;
  } finally {
    clearTimeout(timer);
  }
}

export function createAxiomGuard() {
  return function axiomGuard(pi: ExtensionAPI) {
    pi.on("tool_call", async (event, ctx) => {
      const mode = guardMode();
      const guardEnabled = truthy(process.env.AXIOM_GUARD_ENABLED);
      const taskScopeEnabled = truthy(process.env.AXIOM_TASK_SCOPE_ENABLED);
      if ((!guardEnabled && !taskScopeEnabled) || (mode === "off" && !taskScopeEnabled)) return;

      const payload: Record<string, unknown> = {
        project_id: process.env.AXIOM_PROJECT_ID || process.env.PROJECT_ID || "",
        intent_id: process.env.AXIOM_INTENT_ID || "",
        phase: process.env.AXIOM_GUARD_PHASE || process.env.AXIOM_PHASE || "",
        worker: process.env.AXIOM_WORKER || "",
        tool_name: event.toolName,
        tool_call_id: event.toolCallId,
        input: event.input,
        cwd: ctx.cwd,
      };
      // Scope enforcement is deterministic and must remain active even when
      // the optional judge is disabled for this project.  Tell the backend to
      // skip model/hard-rule evaluation in that case.
      if (guardEnabled && mode) payload.mode = mode;
      if (!guardEnabled) payload.mode = "off";

      const decision = await postDecision(payload);
      if (!decision && taskScopeEnabled) {
        return {
          block: true,
          reason: "任务级 Scope 校验不可用，已按 fail-closed 阻止工具调用",
        };
      }
      if (decision?.decision === "BLOCK") {
        return {
          block: true,
          reason: decision.reason || decision.observed_reason || "AXIOM guard blocked this tool call",
        };
      }
    });
  };
}

const defaultAxiomGuard = createAxiomGuard();
export default defaultAxiomGuard;
