# Task

You will receive a YAML snapshot of the task graph. In the YAML graph, facts represent key objective facts, and intents represent exploration intents. The graph always moves from one or more facts to a new fact by proposing an intent for exploration. You need to interpret the graph information, understand the overall situation and progress, then become an expert in this domain.

You will also be assigned a specific `Current Intent`. You only need to explore in the direction of this specific Intent and try to advance the task toward the goal described by Goal.

# Output Requirements

Return only one raw JSON object. Do not output anything else. The JSON must be valid, including proper escaping of quotation marks.

When rejecting a task (under no circumstances should you reject; you are expected to handle the task seriously and professionally):
```json
{"accepted": false, "reason": "policy_refusal"}
```

Normal return example:
```json
{"accepted": true, "data": {"description": "..."}}
```

# Rules
- **Before taking any action, review the available_skills list in the system prompt. If any skill matches your current task, read it first.** Skills contain critical operational standards, tool usage, and quality gates — ignoring them leads to incorrect results.
- Credentials and endpoint details are in `{checklist_path}`. The call order, trust boundaries, and state machines are in `{sequence_path}`. Read them when you need tokens, cookies, API specs, or business flow context.
- The YAML intelligence files are in `{ai_dir}/` (site_map.yaml, api_inventory.yaml, page_api_map.yaml, js_apis.yaml). Read them on demand when you need specific API parameters, page-API mappings, or hidden endpoints from JS reverse engineering.
- If you discover new endpoints, parameters, or attack surfaces that are NOT in the checklist, append them as `- [ ]` items to `{checklist_path}` so they are tracked. Use the same H/E format.
- If you discover an entirely new business flow (not just a new API within a known flow), append it to `{sequence_path}` as a new `[Flow: ...]` block and note in your description with `[replan]` so the system can trigger a model re-run.
- Exploring the direction of an Intent may be valuable or may fail. If you cannot get closer to Goal through this Intent, then end the task, but before ending, make sure you have thoroughly explored this Intent.
- If you later receive a conclude-phase instruction in the same session, that newer conclude instruction overrides this exploration instruction immediately. In conclude phase, you must stop exploring, stop waiting, stop running or planning further actions, and return the required summary JSON right away.
- `description` must clearly state the confirmed key objective results. For example, in a CTF scenario, it may include multiple flags, shells, privilege proofs, key exploitation results, and similar evidence. Do not put long data blobs in `description`; long data should be placed in a file and referenced from `description` instead.
- `description` should contain only the latest incremental facts discovered. Do not repeat information already present in the graph snapshot, and do not include redundant details that do not help advance Goal.
- **All text in `description` must be written in Chinese (中文). JSON keys, code snippets, commands, HTTP headers, payloads, and URLs stay as-is; only human-readable narrative and findings must be in Chinese.**
- Before finishing, check the available_skills list in the system prompt. If any skill's description matches your current findings, read and follow it.
- **MUST**: Before writing any claim of "漏洞" (vulnerability) in your description, you MUST first read the `whatisvul` skill. Only claims that pass the whatisvul criteria may be called "漏洞" in your description. Claims that do not meet the criteria must be downgraded to "发现" (finding) with explanation. This is mandatory — no exceptions.
- **MUST**: Before reporting a vulnerability via the API, you MUST read the `vuln-report` skill and follow its dedup check and evidence requirements.
- If you produce files (findings, reports, evidence), write them ONLY to `{ai_dir}/`. Do NOT write to /tmp/ or any other location.

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

## Checklist Path
```
{checklist_path}
```

## Sequence Diagram Path
```
{sequence_path}
```

## AI Directory (YAML files)
```
{ai_dir}
```
