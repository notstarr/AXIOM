# Skill 格式规范（pi Agent Skills 标准）

## 文件结构

```
skills/
└── {name}/              ← 目录名 = skill 名称
    └── SKILL.md          ← 固定文件名，大小写敏感
```

pi 通过 `--skill <skills_dir>` 加载时，递归扫描每个子目录，找到 `SKILL.md` 就加载。

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
    <name>vuln-review</name>
    <description>漏洞审核——在上报漏洞前对所有 vuln 声明进行严格审查</description>
    <location>/tmp/.../skills/vuln-review/SKILL.md</location>
  </skill>
</available_skills>
```

3. System prompt 同时告诉模型：
   > "Use the read tool to load a skill's file when the task matches its description."

4. 模型对话中判断 description 匹配 → 调 `read` 工具读取 `SKILL.md` → 加载正文指令

## 命名规范

```
✅ vuln-review
✅ sql-injection-check
✅ flag-submit
✅ red-team-doctrine
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
