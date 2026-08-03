import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { boundedText, errorResult, targetAllowed, textResult } from "../../runtime";

const Parameters = Type.Object(
  {
    url: Type.String({ description: "完整 HTTP 或 HTTPS URL" }),
    method: Type.Optional(Type.Union([
      Type.Literal("GET"), Type.Literal("POST"), Type.Literal("PUT"), Type.Literal("PATCH"),
      Type.Literal("DELETE"), Type.Literal("HEAD"), Type.Literal("OPTIONS"),
    ], { default: "GET" })),
    headers: Type.Optional(Type.Record(Type.String(), Type.String())),
    body: Type.Optional(Type.String({ description: "可选请求正文" })),
    timeout_ms: Type.Optional(Type.Integer({ minimum: 100, maximum: 30000, default: 10000 })),
  },
  { additionalProperties: false },
);

export default function httpRequestExtension(pi: ExtensionAPI) {
  pi.registerTool({
    name: "http_request",
    label: "HTTP 请求",
    description: "在任务 Scope 内发送结构化 HTTP 请求并返回受限响应。",
    promptSnippet: "向任务 Scope 内目标发送结构化 HTTP 请求",
    promptGuidelines: ["使用 http_request 时只访问当前任务 Scope 内的目标；先复用已有漏洞和证据，避免重复请求。"],
    parameters: Parameters,
    async execute(_toolCallId, params, signal) {
      const controller = new AbortController();
      const timeout = Math.min(Math.max(params.timeout_ms || 10000, 100), 30000);
      const timer = setTimeout(() => controller.abort(), timeout);
      const abort = () => controller.abort();
      signal?.addEventListener("abort", abort, { once: true });
      try {
        const parsed = new URL(params.url);
        if (!["http:", "https:"].includes(parsed.protocol)) throw new Error("只支持 HTTP 或 HTTPS URL");
        if (!targetAllowed(parsed.toString())) throw new Error("URL 不在当前任务 Scope 内或已被排除");
        const response = await fetch(parsed, {
          method: params.method || "GET",
          headers: params.headers || {},
          body: ["GET", "HEAD"].includes(params.method || "GET") ? undefined : params.body,
          redirect: "manual",
          signal: controller.signal,
        });
        const body = boundedText(await response.text());
        const headers: Record<string, string> = {};
        response.headers.forEach((value, key) => {
          if (!["set-cookie", "authorization", "proxy-authorization"].includes(key.toLowerCase())) headers[key] = value;
        });
        return textResult(JSON.stringify({
          url: parsed.toString(),
          status: response.status,
          status_text: response.statusText,
          headers,
          body,
          truncated: body.length >= 128000,
        }, null, 2), { status: response.status, response_bytes: Buffer.byteLength(body) });
      } catch (error) {
        return errorResult(error instanceof Error ? error.message : String(error));
      } finally {
        clearTimeout(timer);
        signal?.removeEventListener("abort", abort);
      }
    },
  });
}
