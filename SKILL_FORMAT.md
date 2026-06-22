# Skill 格式规范（pi Agent Skills 标准）

## 文件结构

```
dispatcher/skills/                 ← skills 根目录（与 prompts/ 平级）
└── {name}/                        ← 目录名 = skill 名称
    ├── SKILL.md                   ← 主指令文件（必需）
    ├── references/                ← 参考资料（可选）
    │   ├── command-cheatsheet.md
    │   └── field-spec.md
    ├── scripts/                   ← 脚本（可选）
    │   ├── enum.sh
    │   └── check.py
    └── assets/                    ← 模板等资源（可选）
        └── report-template.md
```

pi 通过 `--skill <skills_dir>` 加载时，递归扫描每个子目录，找到 `SKILL.md` 就加载。
模型可通过 `read` 工具读取 skill 目录下的其他文件（references/、scripts/ 等）。

## SKILL.md 格式

```markdown
---
name: my-skill
description: 一句话描述用途和触发场景
---

## 触发条件

当 AI 准备执行 XX 操作时...

## 操作步骤

1. ...
2. ...

## 规则

- 规则 1
- 规则 2

## 参考资料

- `references/command-cheatsheet.md` — 命令速查
- `references/field-spec.md` — 字段规范
```

## 约束规则（pi 强制校验）

| 字段 | 约束 | 违反后果 |
|------|------|----------|
| `name` | 必须匹配父目录名 | warning |
| `name` | 最长 64 字符 | warning |
| `name` | 只能 `[a-z0-9-]` | warning |
| `name` | 不能以 `-` 开头/结尾 | warning |
| `name` | 不能含 `--` | warning |
| `description` | **必填**，最长 1024 字符 | **没有 description 则 skill 被跳过** |
| `disable-model-invocation` | 可选，设为 true 则只能 `/skill:name` 手动调用 | — |

## 注入机制

1. pi 启动时读取所有 SKILL.md，解析 frontmatter
2. `formatSkillsForPrompt()` 生成 XML 注入 system prompt：

```xml
<available_skills>
  <skill>
    <name>vuln-report</name>
    <description>当成功利用安全漏洞...</description>
    <location>/tmp/.../skills/vuln-report/SKILL.md</location>
  </skill>
</available_skills>
```

3. System prompt 同时告诉模型：
   > "Use the read tool to load a skill's file when the task matches its description."

4. 模型对话中判断 description 匹配 → 调 `read` 工具读取 `SKILL.md` → 加载正文指令
5. 模型可进一步 `read` skill 目录下的 references/、scripts/ 等辅助文件

## 多文件机制

- **SKILL.md**：主指令，存入 DB，前端可编辑
- **references/**、**scripts/**、**assets/**：辅助文件，从磁盘复制到 pi 工作目录
- builtin skill 的辅助文件从 `dispatcher/skills/{name}/` 复制
- custom skill 只有 SKILL.md（前端创建，无辅助文件）
- 前端编辑 SKILL.md 后，下次 worker 启动时覆盖复制后的 SKILL.md，辅助文件不变

## 环境变量

Skills 中可使用以下环境变量：

| 变量 | 说明 |
|------|------|
| `${AXIOM_SERVER}` | AXIOM 服务器地址（如 `http://127.0.0.1:8888`） |
| `${PROJECT_ID}` | 当前任务的项目 ID |

## 命名规范

```
✅ vuln-review
✅ sql-injection-check
✅ flag-submit
✅ subdomain-enum
❌ Vuln-Review        （有大写）
❌ vuln_review        （有下划线）
❌ -vuln-review       （以 - 开头）
❌ vuln--review       （双连字符）
❌ vuln review        （有空格）
```

## description 写法

description 是模型判断是否触发 skill 的**唯一依据**（模型看不到正文，只看到 description）。所以要：

- 写清楚触发场景，而不是 skill 的功能描述
- 用模型能理解的语境匹配关键词

```
✅ "当准备输出 vulns 数组时，对所有漏洞声明进行严格审查"
✅ "在 CTF 比赛中，当需要对某道题提交 flag 时使用"
✅ "当收到 SQL 注入检测任务时，按标准流程逐步测试"
❌ "这是一个漏洞审核 skill"        （太笼统，模型不知道何时触发）
❌ "Skill for checking things"     （不具体）
```
