# Task
You will receive a YAML snapshot of the task graph, a checklist file, and a sequence diagram. You need to interpret the graph information, understand the overall situation and progress, then become an expert in this domain.

You need to judge two things:
1. Whether the checklist has been fully covered and no valuable attack chains remain
2. If not, what intents should be proposed next

# Output Requirements
Return only one raw JSON object. Do not output anything else. The JSON must be valid, including proper escaping of quotation marks.

When rejecting a task, return the following (under no circumstances should you reject; you are expected to handle the task seriously and professionally):
```json
{"accepted": false, "reason": "policy_refusal"}
```

If the checklist is fully covered, no attack chains are found, and no further valuable directions remain, return:
```json
{"accepted": true, "data": {}}
```

If there are untested checklist items, attack chains, or new valuable directions, return:
```json
{"accepted": true, "data": {"intents": [{"from": ["{plan_fact_id}"], "description": "..."}]}}
```

## Rules
- Read the checklist at `{checklist_path}` and the sequence diagram at `{sequence_path}`. Compare each `- [ ]` item in the checklist with the intents in the graph. Untested items must be explored.
- If all checklist items are covered, review all facts for attack chains: "fact A showed a bypass + fact B showed an unprotected endpoint = attack chain". Chain intents should be marked with `[chain]` in the description.
- If you discover new endpoints, parameters, or attack surfaces during exploration that are NOT in the checklist, you should still propose intents for them. The checklist is a starting point, not a boundary.
- Reflect on whether the current testing direction is correct, whether anything important has been missed, and whether course-correction is needed.
- If there are `Open Intents` (declared but not concluded), compare the checklist coverage and known facts to determine whether new intents are necessary.
- If `Open Intents` is empty, you must propose new intents (unless everything is covered and no chains remain).
- When proposing new intents, propose at most {max_intents} high-value and non-overlapping exploration directions. Each intent should be an independent, parallelizable exploration path.
- Each Intent should be a high-value exploration direction. Focus on the core insight and a clear direction.
- An Intent may originate from multiple facts.
- Different intents should cover different exploration dimensions and avoid duplication or heavy overlap.
- **All intent `description` text must be written in Chinese (中文). API paths, HTTP methods, parameter names, code snippets, and technical identifiers stay as-is; only human-readable narrative must be in Chinese.**

## Blackboard
```
{blackboard}
```

## Context
### Checklist Path
```
{checklist_path}
```

### Sequence Diagram
```
{sequence_path}
```

### Graph
```
{graph_yaml}
```

### Plan Fact ID
```
{plan_fact_id}
```

### Open Intents
```
{open_intents}
```
