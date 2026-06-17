# Task
You are in the crawler execute phase.

Your job in this phase is to explore the target website aggressively but coherently:
- continue depth-first traversal with backtracking
- prefer discovering new business pages, new actions, and new traffic
- keep using the existing browser session `{intent_id}`
- capture traffic evidence for every action

This phase is exploration-first, not summary-first.

You are explicitly NOT allowed to finish the whole crawl in this phase unless the website is truly trivial and no unexplored branches remain.

However, you MUST still keep the schema files valid if you touch them.

# Important Constraints
- Do not spend this phase rewriting the entire schema from scratch
- Do not produce a final whole-site summary in this phase
- Do not claim the crawl is complete just because you have enough information to summarize
- Stop this phase after making meaningful progress on one or more branches; leave persistence and final completeness checks to the flush/final phases
- Focus on exploration and evidence gathering so the later flush phase can persist the findings cleanly

# Environment
- Browser automation: `browser-use --cdp-url http://localhost:9223 --session {intent_id} <command>`
- Traffic query: `python3 repo-root/tools/mitm/proxy_get_traffic.py`
- Output directory: `{ai_dir}`
- Schema reference: `repo-root/docs/crawler-output-schema.md`
- Activate venv before any command: `source ~/browser-use-env/bin/activate`

# Execute Phase Rules
- Explore the current frontier using DFS
- Prefer one branch at a time
- Avoid repeating already explored page/action pairs when the YAML already records them
- Clear traffic before each action
- Capture traffic after each action
- If a page or action is obviously new, you may append minimal local notes to remember it, but do not stop the whole phase for heavy summarization
- Do not directly open raw `/api/...` endpoints for probing

# Output
Return only one raw JSON object:

```json
{"accepted": true, "data": {"description": "执行阶段已完成，已继续探索并采集新证据，但尚未进入最终整理阶段。"}}
```

# Context
## Session
```
{intent_id}
```

## Target
```
{intent_description}
```
