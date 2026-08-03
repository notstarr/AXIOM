import fs from "node:fs";
import path from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { errorResult, resolveInsideRun, textResult, walkFiles } from "../../runtime";

const Parameters = Type.Object(
  {
    query: Type.String({ description: "要搜索的文本或正则表达式" }),
    path: Type.Optional(Type.String({ description: "运行目录内的相对路径，默认是运行目录" })),
    regex: Type.Optional(Type.Boolean({ default: false })),
    max_results: Type.Optional(Type.Integer({ minimum: 1, maximum: 200, default: 50 })),
  },
  { additionalProperties: false },
);

export default function searchTextExtension(pi: ExtensionAPI) {
  pi.registerTool({
    name: "search_text",
    label: "搜索文本",
    description: "在本轮运行目录内搜索文本或正则表达式，并返回受限命中摘要。",
    promptSnippet: "搜索本轮运行目录中的文本内容",
    promptGuidelines: ["使用 search_text 查找本轮运行目录中已经产生的文件内容；不要用它替代新的网络测试。"],
    parameters: Parameters,
    async execute(_toolCallId, params, signal) {
      try {
        const root = resolveInsideRun(params.path || ".");
        const expression = params.regex ? new RegExp(params.query, "i") : null;
        const limit = Math.min(Math.max(params.max_results || 50, 1), 200);
        const files = (await fs.promises.stat(root)).isFile() ? [root] : await walkFiles(root);
        const matches: Array<{ path: string; line: number; text: string }> = [];
        let scannedBytes = 0;
        for (const file of files) {
          if (signal?.aborted || matches.length >= limit || scannedBytes >= 16 * 1024 * 1024) break;
          const stat = await fs.promises.stat(file);
          if (stat.size > 2 * 1024 * 1024) continue;
          const content = await fs.promises.readFile(file, "utf8").catch(() => "");
          scannedBytes += Buffer.byteLength(content);
          const lines = content.split(/\r?\n/);
          lines.forEach((line, index) => {
            if (matches.length >= limit) return;
            const found = expression ? expression.test(line) : line.toLowerCase().includes(params.query.toLowerCase());
            expression && (expression.lastIndex = 0);
            if (found) {
              matches.push({
                path: path.relative(resolveInsideRun("."), file) || path.basename(file),
                line: index + 1,
                text: line.slice(0, 500),
              });
            }
          });
        }
        return textResult(JSON.stringify({ matches, truncated: matches.length >= limit }, null, 2), {
          match_count: matches.length,
          scanned_files: files.length,
          scanned_bytes: scannedBytes,
        });
      } catch (error) {
        return errorResult(error instanceof Error ? error.message : String(error));
      }
    },
  });
}
