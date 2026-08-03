import path from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { errorResult, resolveInsideRun, textResult, walkFiles, wildcardPattern } from "../../runtime";

const Parameters = Type.Object(
  {
    pattern: Type.String({ description: "文件名模式，例如 *.json" }),
    path: Type.Optional(Type.String({ description: "运行目录内的相对路径，默认是运行目录" })),
    max_results: Type.Optional(Type.Integer({ minimum: 1, maximum: 500, default: 100 })),
  },
  { additionalProperties: false },
);

export default function findFilesExtension(pi: ExtensionAPI) {
  pi.registerTool({
    name: "find_files",
    label: "查找文件",
    description: "在本轮运行目录内按名称模式查找文件。",
    promptSnippet: "按名称模式查找本轮文件",
    promptGuidelines: ["使用 find_files 先定位文件，再用 read 或 search_text 读取内容。"],
    parameters: Parameters,
    async execute(_toolCallId, params, signal) {
      try {
        const root = resolveInsideRun(params.path || ".");
        const matcher = wildcardPattern(params.pattern);
        const limit = Math.min(Math.max(params.max_results || 100, 1), 500);
        const all = await walkFiles(root);
        const base = resolveInsideRun(".");
        const files: string[] = [];
        for (const file of all) {
          if (signal?.aborted || files.length >= limit) break;
          const relative = path.relative(base, file);
          if (matcher.test(path.basename(file)) || matcher.test(relative)) files.push(relative);
        }
        return textResult(JSON.stringify({ files, truncated: files.length >= limit }, null, 2), {
          result_count: files.length,
        });
      } catch (error) {
        return errorResult(error instanceof Error ? error.message : String(error));
      }
    },
  });
}
