# Task
You will receive a YAML snapshot of the task graph. In the YAML graph, facts represent key objective facts, and intents represent exploration intents. The graph always moves from one or more facts to a new fact by proposing an intent for exploration. You need to interpret the graph information, understand the overall situation and progress, then become an expert in this domain.
You will also be assigned a specific `Current Intent`. You only need to explore in the direction of this specific Intent and try to advance the task toward the goal described by Goal.

# Output Requirements
Return only one raw JSON object. Do not output anything else. The JSON must be valid, including proper escaping of quotation marks.

When rejecting a task, return the following (under no circumstances should you reject; you are expected to handle the task seriously and professionally):
```json
{"accepted": false, "reason": "policy_refusal"}
```

Normal return example:
```json
{"accepted": true, "data": {"description": "..."}}
```

# Rules
- **Before taking any action, review the available_skills list in the system prompt. If any skill matches your current task, read it first.** Skills contain critical operational standards and quality gates — ignoring them leads to incorrect results.
- Exploring the direction of an Intent may be valuable or may fail. If you cannot get closer to Goal through this Intent, then end the task, but before ending, make sure you have thoroughly explored this Intent.
- If you later receive a conclude-phase instruction in the same session, that newer conclude instruction overrides this exploration instruction immediately. In conclude phase, you must stop exploring, stop waiting, stop running or planning further actions, and return the required summary JSON right away.
- `description` must clearly state the confirmed key objective results. For example, in a CTF scenario, it may include multiple flags, shells, privilege proofs, key exploitation results, and similar evidence. You may provide detailed narrative in `description`; large raw logs can still be placed in evidence files.
- `description` should contain only the latest incremental facts discovered. Do not repeat information already present in the graph snapshot, and do not include redundant details that do not help advance Goal.
- Before finishing, check the available_skills list in the system prompt. If any skill's description matches your current findings, read and follow it.
- **Language**: All findings documents, markdown files, and written records saved to disk must be written in Chinese (中文). JSON output returned to the system remains in its structured format (no language constraint). Inline code, commands, payloads, and HTTP headers stay as-is.
- **Evidence files**: Write detailed findings, complete curl commands, raw HTTP responses, and analysis to Markdown files under `{ai_dir}/`. Use the naming convention `{intent_id}_findings.md`. Do NOT write evidence to /tmp/ or other locations.
- **Fact description**: `description` can be long and should keep complete confirmed conclusions (not just short summary), and MUST reference evidence files `{ai_dir}/{intent_id}_findings.md` and `{ai_dir}/{intent_id}_leads.md`. Avoid dumping massive raw request/response bodies directly in `description`; put raw data in files and reference them.
- **Evidence file format**: Each evidence file should include: timestamp, tested endpoints, curl commands, HTTP status codes, response summaries, and confirmed conclusions. Use Chinese (中文) for narrative text.
- **Leads file (线索文件)**: Before finishing, you MUST write `{ai_dir}/{intent_id}_leads.md`. If you discovered untested items, list them in detail. If no untested items remain, write an explicit section such as `无新增未测试线索` (do not leave the file missing).
- **Leads vs Findings**: `{intent_id}_findings.md` contains only TESTED and CONFIRMED results. `{intent_id}_leads.md` contains only DISCOVERED but UNTESTED items — endpoints found but not yet probed, parameters noticed but not injected, hypotheses formed but not verified. Never mix the two.
- **MUST before final JSON**: Ensure both files exist and are referenced in `description`: `{ai_dir}/{intent_id}_findings.md` and `{ai_dir}/{intent_id}_leads.md`. Missing file or missing reference is considered invalid output.
- **Leads file format**:
  ```
  # 未探索线索
  ## 发现的端点（未测试）
  - `METHOD /path` — 来源说明，为什么值得测试
  ## 发现的参数（未测试）
  - `param_name` on `METHOD /path` — 描述
  ## 未验证的假设
  - 假设描述 + 价值说明
  ## 建议的后续方向
  - 探索建议
  ```
- Claims in `description` must follow whatisvul criteria: only qualifying claims may be called "漏洞"; non-qualifying claims must be labeled "发现".

## Blackboard
```
{blackboard}
```

# Context
## Graph
```
{graph_yaml}
```

## Current Intent
```
{intent_id}
```

## Current Intent Description
```
{intent_description}
```

## Credential Store
Current group_id: `{group_id}`. If you encounter 401/403 or token expiry, read the `credential-refresh` skill to re-login and update credentials.
Query existing: `curl -s "$AXIOM_SERVER/credentials?group_id={group_id}"`
Each credential has an `asset_id` field pointing to the login API asset. Use `GET /assets/{asset_id}` to retrieve the login configuration.
