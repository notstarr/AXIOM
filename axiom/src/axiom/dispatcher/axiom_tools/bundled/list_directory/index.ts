import fs from "node:fs";
import path from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { errorResult, resolveInsideRun, textResult } from "../../runtime";

const Parameters = Type.Object(
  {
    path: Type.Optional(Type.String({ description: "运行目录内的相对目录，默认是运行目录" })),
    max_entries: Type.Optional(Type.Integer({ minimum: 1, maximum: 500, default: 100 })),
  },
  { additionalProperties: false },
);

export default function listDirectoryExtension(pi: ExtensionAPI) {
  pi.registerTool({
    name: "list_directory",
    label: "列出目录",
    description: "列出本轮运行目录内一个目录的受限子项。",
    promptSnippet: "列出本轮运行目录的子项",
    promptGuidelines: ["使用 list_directory 了解运行目录结构；不要通过 bash 绕过阶段工具边界。"],
    parameters: Parameters,
    async execute(_toolCallId, params) {
      try {
        const directory = resolveInsideRun(params.path || ".");
        const limit = Math.min(Math.max(params.max_entries || 100, 1), 500);
        const entries = await fs.promises.readdir(directory, { withFileTypes: true });
        const items = entries
          .filter((entry) => !entry.isSymbolicLink())
          .sort((a, b) => a.name.localeCompare(b.name))
          .slice(0, limit)
          .map((entry) => ({
            name: entry.name,
            path: path.relative(resolveInsideRun("."), path.join(directory, entry.name)),
            type: entry.isDirectory() ? "directory" : entry.isFile() ? "file" : "other",
          }));
        return textResult(JSON.stringify({ items, truncated: entries.length > limit }, null, 2), {
          entry_count: items.length,
        });
      } catch (error) {
        return errorResult(error instanceof Error ? error.message : String(error));
      }
    },
  });
}
