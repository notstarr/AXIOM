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
{"accepted": true, "data": {"intents": [{"from": ["f001"], "description": "..."}, {"from": ["f002", "f003"], "description": "..."}]}}
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
- **Each intent should focus on at most 2 related test actions on the same endpoint or attack surface (e.g., test IDOR + parameter tampering on the same API, not IDOR + SSRF + SQLi across different APIs).** Workers have limited time per intent; overloading causes timeouts and incomplete results. If a direction needs more than 2 actions, split into separate intents.
- An Intent may originate from multiple facts.
- Different intents should cover different exploration dimensions and avoid duplication or heavy overlap.
- Before finishing, check the available_skills list in the system prompt. If any skill's description matches your current findings, read and follow it.

## Blackboard
```
{blackboard}
```

## Asset Database
An asset database at `/tmp/axiom-assets.db` tracks discovered API endpoints for the current project. Before proposing intents, you MAY query it to avoid re-testing known endpoints. **Only test endpoints belonging to the current project.**

Use: `sqlite3 /tmp/axiom-assets.db "SELECT api_path, api_method, test_status FROM assets WHERE project_id = 'proj_xxx' ORDER BY api_path LIMIT 15 OFFSET 0;"`

- Always use LIMIT (≤15) for queries — the table may be large. Use OFFSET for pagination.
- If test_status is 'safe' or 'vulnerable', the endpoint has been confirmed and does NOT need a new intent.
- If test_status is 'untested' and there is no open intent for it, consider creating one.
- Always filter by project_id — never query across projects. Replace proj_xxx with the actual project ID from the graph context.

## Context
### Graph
```
{graph_yaml}
```

### Valid facts
```
{fact_ids}
```

### Open Intents
```
{open_intents}
```
