import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import fs from "node:fs";
import path from "node:path";

const MAX_TEXT = 128_000;

export function textResult(text: string, details: Record<string, unknown> = {}) {
  return {
    content: [{ type: "text" as const, text: text.slice(0, MAX_TEXT) }],
    details,
  };
}

export function errorResult(message: string, details: Record<string, unknown> = {}) {
  return {
    content: [{ type: "text" as const, text: `错误：${message}` }],
    details: { ...details, error: message },
    isError: true,
  };
}

export function runDir(): string {
  return process.env.AXIOM_RUN_DIR || process.cwd();
}

export function resolveInsideRun(relativePath = "."): string {
  const root = path.resolve(runDir());
  const resolved = path.resolve(root, relativePath || ".");
  if (resolved !== root && !resolved.startsWith(`${root}${path.sep}`)) {
    throw new Error("路径越出本轮运行目录");
  }
  return resolved;
}

export async function walkFiles(start: string, maxFiles = 10_000): Promise<string[]> {
  const root = path.resolve(start);
  const files: string[] = [];
  const pending = [root];
  while (pending.length && files.length < maxFiles) {
    const directory = pending.pop()!;
    let entries: fs.Dirent[];
    try {
      entries = await fs.promises.readdir(directory, { withFileTypes: true });
    } catch {
      continue;
    }
    for (const entry of entries.sort((a, b) => a.name.localeCompare(b.name))) {
      const item = path.join(directory, entry.name);
      if (entry.isSymbolicLink()) continue;
      if (entry.isDirectory()) pending.push(item);
      else if (entry.isFile()) files.push(item);
      if (files.length >= maxFiles) break;
    }
  }
  return files;
}

export function wildcardPattern(pattern: string): RegExp {
  const escaped = pattern
    .replace(/[.+^${}()|[\]\\]/g, "\\$&")
    .replace(/\*/g, ".*")
    .replace(/\?/g, ".");
  return new RegExp(`^${escaped}$`, "i");
}

export function parseJsonEnv(name: string, fallback: unknown): any {
  try {
    const value = JSON.parse(process.env[name] || "");
    return value ?? fallback;
  } catch {
    return fallback;
  }
}

export function apiBase(): string {
  return (process.env.AXIOM_SERVER || "http://127.0.0.1:8899").replace(/\/$/, "");
}

export async function reportToolEvent(payload: Record<string, unknown>): Promise<void> {
  const token = (process.env.AXIOM_SERVICE_TOKEN || "").trim();
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 1500);
  try {
    await fetch(`${apiBase()}/api/v1/internal/axiom-tools/events`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify({
        task_id: process.env.AXIOM_TASK_ID || "",
        intent_id: process.env.AXIOM_INTENT_ID || "",
        reason_run_id: process.env.AXIOM_REASON_RUN_ID || "",
        review_run_id: process.env.AXIOM_REVIEW_RUN_ID || "",
        lease_id: process.env.AXIOM_LEASE_ID || "",
        worker: process.env.AXIOM_WORKER || "",
        phase: process.env.AXIOM_PHASE || "explore",
        ...payload,
      }),
      signal: controller.signal,
    });
  } catch {
    // Tool telemetry is best effort and must never turn a valid Tool Result
    // into a stage failure.
  } finally {
    clearTimeout(timer);
  }
}

export function registerToolLifecycleAudit(pi: ExtensionAPI, selected: Set<string>): void {
  const started = new Map<string, number>();
  pi.on("tool_execution_start", (event) => {
    if (selected.has(event.toolName)) started.set(event.toolCallId, Date.now());
  });
  pi.on("tool_execution_end", (event) => {
    if (!selected.has(event.toolName)) return;
    const begin = started.get(event.toolCallId) || Date.now();
    started.delete(event.toolCallId);
    void reportToolEvent({
      tool_key: event.toolName,
      event_kind: "execution",
      outcome: event.isError ? "error" : "success",
      duration_ms: Math.max(0, Date.now() - begin),
    });
  });
}

export function boundedText(value: unknown, limit = MAX_TEXT): string {
  return String(value ?? "").slice(0, limit);
}

export function normalizeHost(value: string): string {
  return value.trim().toLowerCase().replace(/\.$/, "");
}

export function targetAllowed(urlText: string): boolean {
  const scope = parseJsonEnv("AXIOM_SCOPE_JSON", {});
  if (!scope || scope.enabled === false) return true;
  const url = new URL(urlText);
  const hostname = normalizeHost(url.hostname);
  const excluded = Array.isArray(scope.exclude) ? scope.exclude.map(normalizeHost) : [];
  if (excluded.some((item: string) => hostname === item || hostname.endsWith(`.${item}`))) return false;
  const targets = Array.isArray(scope.targets) ? scope.targets.map(normalizeHost).filter(Boolean) : [];
  if (!targets.length) return true;
  return targets.some((item: string) => {
    const withoutScheme = item.replace(/^[a-z]+:\/\//, "").split("/")[0].split(":")[0];
    return hostname === withoutScheme || hostname.endsWith(`.${withoutScheme}`);
  });
}
