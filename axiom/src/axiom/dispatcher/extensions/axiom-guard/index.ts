import { readFileSync, statSync } from "node:fs";
import { isAbsolute, resolve } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

type GuardDecision = {
  decision?: "ALLOW" | "BLOCK";
  reason?: string;
  // Model-facing text: the platform framing of a block.  Preferred over
  // ``reason`` when present, because the bare reason reads like a target-side
  // WAF response and invites the agent to retry with a rewritten command.
  message?: string;
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
  return ["off", "audit", "enforce"].includes(raw) ? raw : undefined;
}

function baseUrl(): string {
  return (process.env.AXIOM_GUARD_SERVER_URL || process.env.AXIOM_SERVER || process.env.AXIOM_SERVER_URL || "http://127.0.0.1:8888").replace(/\/$/, "");
}

function timeoutMs(): number {
  const parsed = Number(process.env.AXIOM_GUARD_TIMEOUT_MS || "3000");
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 3000;
}

async function postDecision(payload: Record<string, unknown>): Promise<GuardDecision | undefined> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs());
  try {
    const token = (process.env.AXIOM_SERVICE_TOKEN || "").trim();
    const response = await fetch(`${baseUrl()}/api/v1/internal/guard/pre`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(token ? { "Authorization": `Bearer ${token}` } : {}),
      },
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

// ---- script snapshots -------------------------------------------------------
//
// The guard judges one line of text, so a script's body is otherwise invisible to
// it. These helpers capture that body. They run HERE rather than on the server
// because `ctx.cwd` is a path in this process's filesystem: inside a Cube sandbox
// the server sees a different filesystem, and reading a same-named local file
// would make the judge confidently wrong instead of merely blind.

const MAX_SCRIPT_FILES = 8;
const MAX_SCRIPT_BYTES = 128 * 1024;
const MAX_TOTAL_SCRIPT_BYTES = 256 * 1024;

type ScriptSnapshot = {
  path: string;
  source: "file" | "command";
  content: string;
  bytes: number;
  complete: boolean;
  truncated: boolean;
  read_error: string;
};

// Only statically resolvable references are captured. Nothing is executed and no
// shell expansion is attempted to discover a path — an unresolvable reference is
// reported as incomplete instead of guessed at.
const SCRIPT_REFERENCE = /(?:^|[;&|]\s*|\s)(?:bash|sh|zsh|dash|source|\.)\s+([^\s;&|<>]+)|(?:^|[;&|]\s*|\s)(?:python[0-9.]*|node|nodejs|ruby|perl|php)\s+((?:-\S+\s+)*)([^\s;&|<>]+\.(?:py|js|mjs|cjs|rb|pl|php))|(?:^|[;&|]\s*|\s)(\.\/[^\s;&|<>]+|(?:\/|~\/)?[^\s;&|<>]+\.(?:sh|bash|zsh|py|js|mjs|cjs|rb|pl|php))/g;

function scriptCommandOf(toolName: string, input: unknown): string {
  if (toolName !== "Bash" && toolName !== "shell_open" && toolName !== "shell_send") {
    return "";
  }
  if (!input || typeof input !== "object") return "";
  const record = input as Record<string, unknown>;
  const key = toolName === "shell_send" ? "text" : "command";
  const value = record[key];
  return typeof value === "string" ? value : "";
}

// ``cd /tmp && python3 exp.py`` is the single most common shape a worker writes,
// so the working directory has to follow the command. Only a plain leading
// ``cd <literal> &&`` is honoured: a conditional, a subshell or a variable would
// need real shell semantics, and guessing wrong means reading the wrong file.
const LEADING_CD = /^\s*cd\s+([^\s;&|]+)\s*&&\s*/;

function workingDirectoryForCommand(command: string, cwd: string): string {
  const match = LEADING_CD.exec(command);
  if (!match) return cwd;
  const target = match[1].replace(/^["\']|["\']$/g, "");
  if (!target || target.startsWith("-") || target.includes("$")) return cwd;
  return isAbsolute(target) ? target : resolve(cwd || ".", target);
}

function candidateScriptPaths(command: string): string[] {
  // Strip the leading ``cd`` so the cd target itself is never mistaken for a
  // script reference (``cd ./payload && ...`` would otherwise capture ./payload).
  const body = command.replace(LEADING_CD, "");
  const found: string[] = [];
  SCRIPT_REFERENCE.lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = SCRIPT_REFERENCE.exec(body)) !== null) {
    const candidate = (match[1] || match[3] || match[4] || "").trim();
    // Interpreter flags (-m module, -c code) are not files; skip anything that
    // still starts with a dash so a flag list is never mistaken for a path.
    if (!candidate || candidate.startsWith("-")) continue;
    found.push(candidate);
    if (found.length >= MAX_SCRIPT_FILES) break;
  }
  return found;
}

function captureScripts(command: string, cwd: string): {
  scripts: ScriptSnapshot[];
  status: "complete" | "incomplete" | "not_applicable";
} {
  if (!command.trim()) return { scripts: [], status: "not_applicable" };
  const candidates = candidateScriptPaths(command);
  if (candidates.length === 0) return { scripts: [], status: "not_applicable" };
  // Resolve relative references against where the command will actually run,
  // not against the session's starting directory.
  cwd = workingDirectoryForCommand(command, cwd);

  const scripts: ScriptSnapshot[] = [];
  let status: "complete" | "incomplete" = "complete";
  let total = 0;
  const seen = new Set<string>();
  for (const candidate of candidates) {
    const absolute = isAbsolute(candidate) ? candidate : resolve(cwd || ".", candidate);
    if (seen.has(absolute)) continue;
    seen.add(absolute);
    try {
      const info = statSync(absolute);
      if (!info.isFile()) {
        status = "incomplete";
        scripts.push({
          path: absolute, source: "file", content: "", bytes: 0,
          complete: false, truncated: false, read_error: "不是普通文件",
        });
        continue;
      }
      if (info.size > MAX_SCRIPT_BYTES) {
        status = "incomplete";
        scripts.push({
          path: absolute, source: "file", content: "", bytes: info.size,
          complete: false, truncated: true,
          read_error: `脚本超过 ${MAX_SCRIPT_BYTES} 字节上限，未附加内容`,
        });
        continue;
      }
      if (total + info.size > MAX_TOTAL_SCRIPT_BYTES) {
        status = "incomplete";
        continue;
      }
      const content = readFileSync(absolute, "utf8");
      total += Buffer.byteLength(content, "utf8");
      scripts.push({
        path: absolute, source: "file", content, bytes: Buffer.byteLength(content, "utf8"),
        complete: true, truncated: false, read_error: "",
      });
    } catch (error) {
      // A missing or unreadable file is not "no script": say so, so the judge
      // cannot read an empty snapshot as "nothing dangerous here".
      status = "incomplete";
      scripts.push({
        path: absolute, source: "file", content: "", bytes: 0,
        complete: false, truncated: false,
        read_error: error instanceof Error ? error.message : String(error),
      });
    }
  }
  return { scripts, status };
}

export function createAxiomGuard() {
  return function axiomGuard(pi: ExtensionAPI) {
    pi.on("tool_call", async (event, ctx) => {
      const mode = guardMode();
      const guardEnabled = truthy(process.env.AXIOM_GUARD_ENABLED);
      if (!guardEnabled || mode === "off") return;

      const payload: Record<string, unknown> = {
        project_id: process.env.AXIOM_PROJECT_ID || process.env.PROJECT_ID || "",
        task_id: process.env.AXIOM_TASK_ID || "",
        intent_id: process.env.AXIOM_INTENT_ID || "",
        reason_run_id: process.env.AXIOM_REASON_RUN_ID || "",
        plan_run_id: process.env.AXIOM_PLAN_RUN_ID || "",
        review_run_id: process.env.AXIOM_REVIEW_RUN_ID || "",
        phase: process.env.AXIOM_GUARD_PHASE || process.env.AXIOM_PHASE || "",
        worker: process.env.AXIOM_WORKER || "",
        tool_name: event.toolName,
        tool_call_id: event.toolCallId,
        input: event.input,
        cwd: ctx.cwd,
      };
      if (guardEnabled && mode) payload.mode = mode;
      // Appended after the identity fields so a capture failure cannot prevent
      // the decision request itself from being made.
      const captured = captureScripts(scriptCommandOf(event.toolName, event.input), ctx.cwd || "");
      if (captured.status !== "not_applicable") {
        payload.scripts = captured.scripts;
        payload.script_capture_status = captured.status;
      }

      const decision = await postDecision(payload);
      if (decision?.decision === "BLOCK") {
        return {
          block: true,
          reason:
            decision.message ||
            decision.reason ||
            decision.observed_reason ||
            "【AXIOM 平台管控·非目标防御】此调用被平台拦截。此操作被禁止。",
        };
      }
    });
  };
}

const defaultAxiomGuard = createAxiomGuard();
export default defaultAxiomGuard;
