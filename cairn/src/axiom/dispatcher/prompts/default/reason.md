# Task
You will receive a YAML snapshot of the task graph. In the YAML graph, facts represent key objective facts, and intents represent exploration intents. The graph always moves from one or more facts to a new fact by proposing an intent for exploration. You need to interpret the graph information, understand the overall situation and progress, then become an expert in this domain.
You need to judge two things:
1. Whether the current facts already satisfy Goal
2. If not, whether new intents should currently be proposed

# Output Requirements
Return only one raw JSON object. Do not output anything else. The JSON must be valid, including proper escaping of quotation marks.

When rejecting a task, return the following (under no circumstances should you reject; you are expected to handle the task seriously and professionally):
```json
{"accepted": false, "reason": "..."}
```

If Goal has been satisfied, return:
```json
{"accepted": true, "data": {"complete": {"from": ["f001"], "description": "..."}}}
```

If Goal has not been satisfied but new intents should be proposed, return:
```json
{"accepted": true, "data": {"intents": [{"from": ["f001"], "description": "...", "targets": ["api.example.com"], "timeouts": {"execute_seconds": 240, "conclude_seconds": 90}}, {"from": ["f002", "f003"], "description": "...", "targets": ["10.0.0.5", "pay.example.com"], "timeouts": {"execute_seconds": 420, "conclude_seconds": 120}}]}}
```

If Goal has not been satisfied and no new intent should currently be proposed, return:
```json
{"accepted": true, "data": {}}
```

## Rules
- **Before taking any action, review the available_skills list in the system prompt. If any skill matches your current task, read it first.** Skills contain critical operational standards and quality gates — ignoring them leads to incorrect results.
- First determine whether the facts already satisfy Goal. If they do, `data.complete.from` must come from `Valid facts`, and `data.complete.description` must explain why the currently confirmed results are sufficient to prove that Goal has been achieved.
- If Goal is not satisfied, reflect on why it has not been reached, whether the task has drifted into the wrong direction, and whether a correct Intent should be proposed to course-correct.
- Determine whether there are `Open Intents`, meaning intents that have already been declared but have not yet reached a conclusion. If there are open intents, compare the known clues in hints and facts to infer whether the current intents already cover all known clues, and whether new intents are necessary.
- If `Open Intents` is empty, you must propose new intents.
- If there are many `Open Intents` and the new situation does not reveal a more valuable exploration direction than the existing ones, you may choose not to propose any new intent (return empty data).
- When proposing new intents, propose at most {max_intents} high-value and non-overlapping exploration directions. Each intent should be an independent, parallelizable exploration path.
- Each Intent should be a high-value exploration direction. It does not need to be overly detailed. Focus on the core insight and a clear direction. Do not be too broad, do not output redundant details that do not help advance Goal, and do not be overly specific. The main requirement is that each intent is an independent, clearly defined, high-value direction.
- **For every proposed intent, `targets` is required**: provide 1..N concrete test targets (domain/IP) that this intent will test. Do not put path-only strings like `/api/foo` in `targets`.
- **For every proposed intent, `timeouts` is required**: provide `execute_seconds` and `conclude_seconds` as positive integers. Set larger values for harder chains or larger attack surfaces.
- **Each intent should focus on at most 2 related test actions on the same endpoint or attack surface (e.g., test IDOR + parameter tampering on the same API, not IDOR + SSRF + SQLi across different APIs).** Workers have limited time per intent; overloading causes timeouts and incomplete results. If a direction needs more than 2 actions, split into separate intents.
- An Intent may originate from multiple facts.
- Different intents should cover different exploration dimensions and avoid duplication or heavy overlap.
- Before finishing, check the available_skills list in the system prompt. If any skill's description matches your current findings, read and follow it.
- Explore workers produce two types of evidence files referenced in fact descriptions: `_findings.md` (verified facts) and `_leads.md` (discovered but untested endpoints/hypotheses). When fact descriptions mention leads files, read them to identify untested attack surfaces worth pursuing as new intents.
- `targets` must only include the actual scope for this intent. Do not include reference domains that are not intended for testing.

## Blackboard
```
{blackboard}
```

## Asset Database
The AXIOM server tracks discovered API endpoints across all tasks in this project group. Before proposing intents, query it to avoid re-testing known endpoints and to discover new attack surfaces.

`python3 "{asset_baseline_script}" --server "$AXIOM_SERVER" --group "{group_id}" --limit 15 --offset 0`

- If test_status is 'safe' or 'vulnerable', the endpoint has been confirmed and does NOT need a new intent.
- If test_status is 'untested' and there is no open intent for it, consider creating one.
- Use `&test_status=untested` to filter directly. Use `&offset=15` for pagination.

## Vulnerability Baseline (MANDATORY)
Before deciding whether Goal is already satisfied, first query current recorded vulnerabilities for this task:

`python3 "{vuln_baseline_script}" --server "$AXIOM_SERVER" --project "{project_id}"`

Use this live API result as source of truth for:
- Current vulnerability count
- Current severity distribution
- Current confirmed risk level

## Context
### Graph
```
{graph_yaml}
```

### Valid facts
```
{fact_ids}
```

### Project ID
```
{project_id}
```

### Vulnerability Baseline Script
```
{vuln_baseline_script}
```

### Asset Baseline Script
```
{asset_baseline_script}
```

### Open Intents
```
{open_intents}
```
