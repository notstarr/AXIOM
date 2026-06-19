# Task
You will receive a YAML snapshot of the task graph, a checklist file, and a sequence diagram. Your job has two phases:

Phase 1 — Checklist coverage: compare the checklist with the graph to see what's been tested and what hasn't.
Phase 2 — Attack chaining: if all checklist items are covered, analyze the facts within the plan's scope to find multi-step attack chains.

# Steps

1. Read the checklist at `{checklist_path}` and the sequence diagram at `{sequence_path}`
2. Compare each `- [ ]` item in the checklist with the intents in the graph YAML
3. For items that have NO corresponding intent → create a new intent (skip to output)
4. If ALL items are covered → move to Phase 2: chain analysis

## Phase 2 — Chain Analysis
- Review all facts produced within this plan's scope
- Look for combinations: "fact A showed a bypass + fact B showed an unprotected endpoint = attack chain"
- If you find a valid chain → create intents to test the chain (mark each intent description with `[chain]`)
- If no valid chain is found → return empty data (plan complete)
- Do NOT search for new attack surfaces outside the plan's scope

# Output

Untested checklist item:
```json
{"accepted": true, "data": {"intents": [{"from": ["{plan_fact_id}"], "description": "Test IDOR on POST /api/xxx"}]}}
```

Attack chain found:
```json
{"accepted": true, "data": {"intents": [{"from": ["f003", "f005"], "description": "[chain] Combine limit bypass with buynow logic flaw to test stock exhaustion"}]}}
```

All tested, no chains:
```json
{"accepted": true, "data": {}}
```

# Rules
- Create one intent per untested checklist item
- In Phase 1, do NOT propose intents unrelated to checklist items
- In Phase 2, stay within facts discovered in this plan — do NOT go beyond the plan's scope
- Create at most {max_intents} intent(s) per call; you will be called again for remaining items
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
