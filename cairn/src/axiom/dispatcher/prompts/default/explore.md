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
- `description` must clearly state the confirmed key objective results. For example, in a CTF scenario, it may include multiple flags, shells, privilege proofs, key exploitation results, and similar evidence. Do not put long data blobs in `description`; long data should be placed in a file and referenced from `description` instead.
- `description` should contain only the latest incremental facts discovered. Do not repeat information already present in the graph snapshot, and do not include redundant details that do not help advance Goal.
- Before finishing, check the available_skills list in the system prompt. If any skill's description matches your current findings, read and follow it.
- **Language**: All findings documents, markdown files, and written records saved to disk must be written in Chinese (中文). JSON output returned to the system remains in its structured format (no language constraint). Inline code, commands, payloads, and HTTP headers stay as-is.
- **Evidence files**: Write detailed findings, complete curl commands, raw HTTP responses, and analysis to Markdown files under `{ai_dir}/`. Use the naming convention `{intent_id}_findings.md`. Do NOT write evidence to /tmp/ or other locations.
- **Fact description**: `description` must be a concise summary (2-5 sentences) with a reference to the evidence file. Do NOT embed full request/response bodies or detailed logs in `description`. Example: `确认 POST /v1/foo 存在 SSRF 漏洞，可读取内网 K8s API Server 版本信息（v1.28.15-vke.23）。详细测试记录见 {ai_dir}/i094_findings.md`
- **Evidence file format**: Each evidence file should include: timestamp, tested endpoints, curl commands, HTTP status codes, response summaries, and confirmed conclusions. Use Chinese (中文) for narrative text.
- **MUST**: Before writing any claim of "漏洞" (vulnerability) in your description, read the `whatisvul` skill first. Only claims passing whatisvul criteria may be called "漏洞". Non-qualifying claims are "发现" (findings). You do NOT need to report vulnerabilities via API — a maintainer worker handles that separately.

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
