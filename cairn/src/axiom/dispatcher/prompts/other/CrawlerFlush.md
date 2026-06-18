# Task
You are in the crawler flush phase.

This phase overrides any earlier instruction in the same session that told you to keep exploring, keep clicking, keep opening new pages, or continue broad crawling.

Your job in this phase is NOT to continue broad exploration.
Your job is to persist already confirmed discoveries from the recent crawl phase into the schema files.

You must update these files in `{ai_dir}` according to:
- `repo-root/docs/crawler-output-schema.md`

Target files:
- `site_map.yaml`
- `api_inventory.yaml`
- `page_api_map.yaml`

# Flush Phase Rules
- Do not open distant new branches just to keep exploring
- Do not continue normal crawling in this phase
- Do not use browser automation for fresh exploration in this phase
- You may inspect the current page, read the existing YAML, and read the latest captured traffic
- You may perform only the minimum local inspection needed to finish writing records cleanly
- Every page already reached in the current session but not yet written must be written now
- Every API already observed in traffic but not yet written must be written now
- Every user action already performed but not yet mapped must be written now
- If a request failed (401/403/404/500/validation failure), record it as a failed action or failed API observation instead of dropping it

Required flush checklist:
- If `site_map.yaml` does not exist, create it now with schema v1 skeleton
- If `api_inventory.yaml` does not exist, create it now with schema v1 skeleton
- If `page_api_map.yaml` does not exist, create it now with schema v1 skeleton
- If the entry page has been visited but `site_map.yaml.pages` is empty, write the entry page immediately
- If any page has been visited in this session, `site_map.yaml.pages` must not remain empty when you finish flush
- If any API has been seen in traffic, `api_inventory.yaml.apis` must not remain empty when you finish flush
- If any user action has been performed, `page_api_map.yaml.mappings` must not remain empty when you finish flush
- If you know less than expected, write partial but valid schema records instead of leaving the files empty

Allowed actions in this phase:
- read existing YAML files
- inspect current browser/session state only if required for naming or mapping
- query latest traffic
- create or overwrite schema files
- append missing schema records

Disallowed actions in this phase:
- opening new business pages to continue exploration
- navigating to new branches
- broad clicking loops
- screenshot loops unless a screenshot is strictly required to name an already visited page
- repeating crawl-like exploration just to gather more evidence
- trying to finish the whole site from scratch in this phase

Incremental-only rule:
- Focus only on what is newly confirmed since the previous flush or since the current round started
- Prefer appending or updating missing records over rewriting the whole schema
- Do not throw away valid existing records
- Do not restate the entire world if only a few new facts were discovered

# Hard Requirement
Do not leave the three schema files as stale snapshots if newer evidence already exists in the browser session or traffic log.

Do not finish this phase with skeleton-only files if the browser has already visited one or more real pages.

Minimum acceptable result for this flush phase:
- at least one concrete page record in `site_map.yaml`
- at least one concrete action or on-load mapping in `page_api_map.yaml` once actions or page-load traffic exist
- at least one concrete API record in `api_inventory.yaml` once API traffic exists

# Environment
- Browser automation: `browser-use --cdp-url http://localhost:9223 --session {intent_id} <command>`
- Traffic query: `python3 repo-root/tools/mitm/proxy_get_traffic.py`
- Output directory: `{ai_dir}`
- Schema reference: `repo-root/docs/crawler-output-schema.md`
- Activate venv before any command: `source ~/browser-use-env/bin/activate`

# Output
Return only one raw JSON object:

```json
{"accepted": true, "data": {"description": "整理阶段已完成，本轮新增发现已写入 schema 文件。"}}
```

If you cannot produce concrete schema records, do not pretend flush succeeded. Write the records first.

Your `description` should summarize only the incremental records written in this flush, such as:
- how many new pages were added
- how many new APIs were added
- how many new page-to-API mappings were added
- any important failed actions that were recorded

# Context
## Session
```
{intent_id}
```

## Target
```
{intent_description}
```
